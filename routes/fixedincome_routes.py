"""固定收益证券分析路由 — 债券定价/久期凸性、组合、收益率曲线、信用利差、压力测试"""
from core_config import exc_response
import csv
import io
import json
import traceback
from flask import request, jsonify, Blueprint

from core_config import (
    OUT_DIR, logger, load_dataframe,
    pd, np, os,
    _get_csv_file,
)


# ============================================================
#  核心财务计算
# ============================================================
def _bond_metrics(face, coupon_pct, ytm_pct, maturity, freq=2, accrual_frac=0.0):
    """单只债券核心指标：价格、麦考利久期、修正久期、凸性、DV01、应计利息。

    face: 面值(默认100)  coupon_pct/ytm_pct: 年化百分比  maturity: 年
    freq: 每年付息次数  accrual_frac: 已计息期占比(0~1)，用于应计利息
    """
    c = float(coupon_pct) / 100.0
    y = float(ytm_pct) / 100.0
    m = max(1, int(freq))
    n = max(1, int(round(float(maturity) * m)))
    r = y / m
    pm = float(face) * c / m  # 每期票息
    times = list(range(1, n + 1))
    cf = [pm] * n
    cf[-1] += float(face)
    pv = [cf[t - 1] / ((1 + r) ** t) for t in times]
    price = sum(pv)  # 每百元面值净价
    if price <= 0 or (1 + r) <= 0:
        return None
    mac_per = sum(t * pv[t - 1] for t in times) / price          # 麦考利久期(期)
    mac_years = mac_per / m                                       # 年
    mod_dur = mac_per / (m * (1 + r)) if (m * (1 + r)) != 0 else 0  # 修正久期(年)
    conv_sum = sum(t * (t + 1) * pv[t - 1] for t in times)
    convexity = conv_sum / (price * (1 + r) ** 2) / (m ** 2)      # 年化凸性
    dv01 = mod_dur * price * 0.0001                               # 每百元面值 DV01
    accrued = pm * float(accrual_frac)
    return {
        "face": float(face),
        "coupon_pct": float(coupon_pct),
        "ytm_pct": float(ytm_pct),
        "maturity": float(maturity),
        "freq": m,
        "price": round(price, 4),
        "clean_price": round(price - accrued, 4),
        "accrued": round(accrued, 4),
        "macaulay_duration_years": round(mac_years, 4),
        "modified_duration": round(mod_dur, 4),
        "convexity": round(convexity, 4),
        "dv01": round(dv01, 4),
    }


def _parse_tenor(t):
    """把 '3M'/'2Y'/'10'/'5.5' 等解析为年(float)"""
    if t is None:
        return None
    s = str(t).strip().upper().replace("年", "").replace("Y", "").replace("M", "")
    try:
        return float(s) / 12.0 if str(t).strip().upper().endswith("M") else float(s)
    except ValueError:
        return None


def _analyze_curve(points):
    """points: [{tenor, yield}] -> 形态判定 + 关键期限利差"""
    pts = []
    for p in points:
        yr = _parse_tenor(p.get("tenor"))
        yld = p.get("yield")
        if yr is None or yld is None:
            continue
        try:
            pts.append((float(yr), float(yld)))
        except (TypeError, ValueError):
            continue
    if len(pts) < 2:
        return {"error": "收益率曲线至少需要 2 个有效点"}
    pts.sort(key=lambda x: x[0])
    short_y, short_v = pts[0]
    long_y, long_v = pts[-1]
    spread = long_v - short_v
    if spread > 0.5:
        shape = "陡峭 / 正常（长端高于短端，经济扩张）"
    elif spread < -0.5:
        shape = "倒挂（长端低于短端，衰退预警信号）"
    else:
        shape = "平坦（曲线走平，政策转向或降温）"

    # 关键期限利差
    by_year = {yr: v for yr, v in pts}
    spreads = {}
    pairs = [("10Y-2Y", 10, 2), ("10Y-3M", 10, 0.25), ("30Y-5Y", 30, 5), ("5Y-2Y", 5, 2)]
    for label, a, b in pairs:
        if a in by_year and b in by_year:
            spreads[label] = round(by_year[a] - by_year[b], 4)

    return {
        "shape": shape,
        "short_long_spread": round(spread, 4),
        "points_count": len(pts),
        "key_spreads": spreads,
        "note": "利差转负即倒挂；常与衰退领先 12-18 个月相关，需多指标交叉验证。",
    }


def _stress(bonds, shocks_bp):
    """组合利率平行冲击压力测试。bonds: 含 metrics + market_value"""
    total_mv = sum(float(b.get("market_value") or 0) for b in bonds)
    if total_mv <= 0:
        return {"error": "组合市值必须大于 0"}
    w_mod = sum((float(b["market_value"]) / total_mv) * b["metrics"]["modified_duration"] for b in bonds)
    w_conv = sum((float(b["market_value"]) / total_mv) * b["metrics"]["convexity"] for b in bonds)
    out = []
    for s in shocks_bp:
        ds = float(s) / 10000.0
        d_pct = -w_mod * ds + 0.5 * w_conv * (ds ** 2)
        out.append({
            "shock_bp": float(s),
            "delta_pct": round(d_pct * 100, 4),
            "new_value": round(total_mv * (1 + d_pct), 2),
        })
    return {
        "portfolio_modified_duration": round(w_mod, 4),
        "portfolio_convexity": round(w_conv, 4),
        "portfolio_market_value": round(total_mv, 2),
        "scenarios": out,
    }


def _build_bond(rec, idx):
    """构造单只债券输出（含 metrics 或 error）"""
    name = rec.get("name") or f"债券{idx+1}"
    try:
        mv = float(rec.get("market_value") or 100)
        met = _bond_metrics(
            face=rec.get("face", 100),
            coupon_pct=rec.get("coupon"),
            ytm_pct=rec.get("ytm"),
            maturity=rec.get("maturity"),
            freq=rec.get("freq", 2),
            accrual_frac=rec.get("accrual_frac", 0.0),
        )
        if met is None:
            return {"name": name, "error": "无法计算(价格/收益异常)"}
        return {"name": name, "market_value": mv, "metrics": met}
    except (TypeError, ValueError) as e:
        return {"name": name, "error": f"参数错误: {e}"}


def _bonds_from_json(body):
    return [_build_bond(b, i) for i, b in enumerate(body.get("bonds") or [])]


def _bonds_from_csv(text):
    bonds = []
    reader = csv.DictReader(io.StringIO(text))
    for i, row in enumerate(reader):
        def g(k):
            for key in (k, k.lower(), k.upper()):
                if key in row and str(row[key]).strip() != "":
                    return row[key]
            return ""
        try:
            bonds.append(_build_bond({
                "name": g("name") or g("名称"),
                "face": float(g("face") or g("面值") or 100),
                "coupon": float(g("coupon") or g("票息") or g("票息率")),
                "ytm": float(g("ytm") or g("收益率") or g("到期收益率")),
                "maturity": float(g("maturity") or g("期限") or g("maturity_years")),
                "freq": int(float(g("freq") or g("付息次数") or 2)),
                "market_value": float(g("market_value") or g("市值") or g("权重") or 100),
                "accrual_frac": float(g("accrual_frac") or 0.0),
            }, i))
        except (TypeError, ValueError):
            continue
    return bonds


# ============================================================
#  路由注册
# ============================================================
def register_routes(app, limiter):
    bp = Blueprint('fixedincome_routes', __name__)
    """注册 /api/fixedincome/* 固定收益分析接口"""

    # /api/fixedincome/bond 单债计算
    @bp.route("/api/fixedincome/bond", methods=["POST"])
    @limiter.limit("10 per minute")
    def fi_bond():
        try:
            d = request.json or {}
            for k in ("coupon", "ytm", "maturity"):
                if d.get(k) in (None, ""):
                    return jsonify({"error": f"缺少必填参数: {k}"}), 400
            met = _bond_metrics(
                face=d.get("face", 100),
                coupon_pct=d.get("coupon"),
                ytm_pct=d.get("ytm"),
                maturity=d.get("maturity"),
                freq=d.get("freq", 2),
                accrual_frac=d.get("accrual_frac", 0.0),
            )
            if met is None:
                return jsonify({"error": "无法计算：价格或收益参数异常（检查 ytm/票息/期限）"}), 400
            return jsonify({"ok": True, "metrics": met})
        except Exception as e:
            logger.error("fi_bond error: %s\n%s", e, traceback.format_exc())
            return exc_response(e)

    # /api/fixedincome/portfolio 组合分析
    @bp.route("/api/fixedincome/portfolio", methods=["POST"])
    @limiter.limit("10 per minute")
    def fi_portfolio():
        try:
            raw = request.get_json(silent=True) or {}
            bonds = _bonds_from_json(raw)
            if not bonds and raw.get("csv"):
                bonds = _bonds_from_csv(raw["csv"])
            if not bonds:
                return jsonify({"error": "请提供 bonds 列表或 csv 文本"}), 400
            valid = [b for b in bonds if "metrics" in b]
            if not valid:
                return jsonify({"error": "无有效债券数据", "details": bonds}), 400
            total_mv = sum(b["market_value"] for b in valid)
            port_mod = sum((b["market_value"] / total_mv) * b["metrics"]["modified_duration"] for b in valid)
            port_conv = sum((b["market_value"] / total_mv) * b["metrics"]["convexity"] for b in valid)
            port_dv01 = port_mod * total_mv * 0.0001
            return jsonify({
                "ok": True,
                "bonds": bonds,
                "portfolio": {
                    "modified_duration": round(port_mod, 4),
                    "convexity": round(port_conv, 4),
                    "dv01_currency": round(port_dv01, 4),
                    "market_value": round(total_mv, 2),
                },
            })
        except Exception as e:
            logger.error("fi_portfolio error: %s\n%s", e, traceback.format_exc())
            return exc_response(e)

    # /api/fixedincome/curve 收益率曲线
    @bp.route("/api/fixedincome/curve", methods=["POST"])
    @limiter.limit("10 per minute")
    def fi_curve():
        try:
            d = request.json or {}
            points = d.get("points") or []
            if not points:
                # 允许直接传 {tenor: yield} 字典
                if isinstance(d.get("curve"), dict):
                    points = [{"tenor": k, "yield": v} for k, v in d["curve"].items()]
            res = _analyze_curve(points)
            if "error" in res:
                return jsonify(res), 400
            return jsonify({"ok": True, **res})
        except Exception as e:
            logger.error("fi_curve error: %s\n%s", e, traceback.format_exc())
            return exc_response(e)

    # /api/fixedincome/spread 信用利差 / OAS
    @bp.route("/api/fixedincome/spread", methods=["POST"])
    @limiter.limit("30 per minute")
    def fi_spread():
        try:
            d = request.json or {}
            credit = d.get("credit_ytm")
            bench = d.get("benchmark_ytm")
            if credit is None or bench is None:
                return jsonify({"error": "缺少 credit_ytm 或 benchmark_ytm"}), 400
            credit = float(credit); bench = float(bench)
            spread = round(credit - bench, 4)
            opt_cost = float(d.get("option_cost") or 0)
            oas = round(spread - opt_cost, 4)
            pd = d.get("pd"); lgd = d.get("lgd")
            exp_loss = None
            if pd is not None and lgd is not None:
                exp_loss = round(float(pd) * float(lgd), 6)
            if spread > 0.5:
                assess = "利差偏宽，反映风险溢价上升/避险情绪"
            elif spread < 0:
                assess = "利差为负（信用债收益低于同期限国债），异常"
            else:
                assess = "利差偏窄，风险偏好较高"
            return jsonify({
                "ok": True,
                "spread": spread,
                "oas": oas,
                "expected_loss_pd_x_lgd": exp_loss,
                "assessment": assess,
            })
        except Exception as e:
            logger.error("fi_spread error: %s\n%s", e, traceback.format_exc())
            return exc_response(e)

    # /api/fixedincome/stress 压力测试
    @bp.route("/api/fixedincome/stress", methods=["POST"])
    @limiter.limit("10 per minute")
    def fi_stress():
        try:
            raw = request.get_json(silent=True) or {}
            bonds = _bonds_from_json(raw)
            if not bonds and raw.get("csv"):
                bonds = _bonds_from_csv(raw["csv"])
            valid = [b for b in bonds if "metrics" in b]
            if not valid:
                return jsonify({"error": "请提供有效的组合 bonds 或 csv"}), 400
            shocks = raw.get("shocks") or [50, -25, 100]
            res = _stress(valid, shocks)
            if "error" in res:
                return jsonify(res), 400
            return jsonify({"ok": True, **res})
        except Exception as e:
            logger.error("fi_stress error: %s\n%s", e, traceback.format_exc())
            return exc_response(e)
    app.register_blueprint(bp)
