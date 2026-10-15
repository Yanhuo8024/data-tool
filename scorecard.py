#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scorecard.py - 信用评分卡建模（标准风控流程）

流程：变量分箱(等频/等距/卡方) → WOE/IV 计算与筛选 → 逻辑回归
      → 评分刻度映射(Score = A - B·ln(odds)) → 评估(AUC/KS/混淆矩阵) → PSI 稳定性

评分刻度：
    B = PDO / ln(2)
    A = 基准分 + B·ln(基准odds)
    logit = intercept + Σ coef_j · WOE_j(x)
    score = A - B · logit

分箱结果携带可复现的边界（assign），保证训练与上线打分用同一套分箱规则：
    numeric   -> {'kind': 'numeric',  'edges': [...], 'woes': [...]}
    category  -> {'kind': 'category', 'groups': [...], 'woes': [...]}
"""
import json
import math
import os
from datetime import datetime
import numpy as np
import pandas as pd

EPS = 1e-6

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output', 'models')


def _chi2_table(good_bad):
    from scipy.stats import chi2_contingency
    try:
        chi2, _, _, _ = chi2_contingency(good_bad)
        return chi2
    except Exception:
        return 1e9


def _chi2_merge(df, max_bins):
    """卡方合并相邻箱。df 需含 Interval 列 iv（按 left 排序）与 good/bad 列"""
    df = df.sort_values('left').reset_index(drop=True)
    while len(df) > max_bins:
        best = None
        for i in range(len(df) - 1):
            table = [[df.loc[i, 'good'], df.loc[i, 'bad']],
                     [df.loc[i + 1, 'good'], df.loc[i + 1, 'bad']]]
            chi2 = _chi2_table(table)
            if best is None or chi2 < best[0]:
                best = (chi2, i)
        if best is None:
            break
        i = best[1]
        iv1, iv2 = df.loc[i, 'iv'], df.loc[i + 1, 'iv']
        merged_iv = pd.Interval(iv1.left, iv2.right,
                                closed='left' if iv1.closed == 'left' else 'right')
        merged = pd.DataFrame([{
            'iv': merged_iv,
            'left': merged_iv.left,
            'good': df.loc[i, 'good'] + df.loc[i + 1, 'good'],
            'bad': df.loc[i, 'bad'] + df.loc[i + 1, 'bad'],
        }])
        df = pd.concat([df.iloc[:i], merged, df.iloc[i + 2:]], ignore_index=True)
        df = df.sort_values('left').reset_index(drop=True)
    return df


def _make_table(assign, total_good, total_bad):
    """从 assign 结构生成 woe 表 rows，并回填 assign['woes']"""
    rows = []
    woes = []
    keys = []
    for item in assign['items']:
        bad = int(item['bad'])
        good = int(item['good'])
        bad_p = (bad + 0.5) / max(total_bad + 0.5, EPS)
        good_p = (good + 0.5) / max(total_good + 0.5, EPS)
        woe = float(np.log(bad_p / good_p))
        iv = (bad_p - good_p) * woe
        woes.append(round(woe, 4))
        keys.append(item['label'])
        rows.append({
            'bin': item['label'],
            'count': int(bad + good),
            'good': int(good),
            'bad': int(bad),
            'bad_rate': round(bad / max(bad + good, 1), 4),
            'woe': round(woe, 4),
            'iv': round(iv, 4),
        })
    assign['keys'] = keys
    assign['woes'] = woes
    assign['iv_total'] = round(sum(r['iv'] for r in rows), 4)
    return rows


def _merge_to_monotonic(items: list) -> list:
    """单调性合并：迭代合并破坏单调的相邻箱，直到 WOE 单调（或仅剩 2 箱）。
    仅用于 numeric items（含 left/right）。WOE 随 x 单调（升或降均可）。"""
    if len(items) <= 2:
        return items
    items = [dict(i) for i in items]
    while len(items) > 2:
        def _woe(g, b):
            return float(np.log((b + 0.5) / max(g + 0.5, EPS)))
        woes = [_woe(i['good'], i['bad']) for i in items]
        trend = woes[-1] - woes[0]  # 整体方向
        if abs(trend) < 1e-9:
            trend = 1.0
        bad_idx = None
        for i in range(len(items) - 1):
            d = woes[i + 1] - woes[i]
            if d * trend < 0:  # 与整体方向相反 → 破坏单调
                bad_idx = i
                break
        if bad_idx is None:
            break
        a, b = items[bad_idx], items[bad_idx + 1]
        merged = {
            'label': f"[{a.get('left', '?'):g}, {b.get('right', '?'):g}]",
            'left': a.get('left'), 'right': b.get('right'),
            'good': a['good'] + b['good'],
            'bad': a['bad'] + b['bad'],
        }
        items = items[:bad_idx] + [merged] + items[bad_idx + 2:]
    return items


def bin_series(s: pd.Series, y: pd.Series, method: str = 'chimerge',
               max_bins: int = 5, q_init: int = 20, monotonic: bool = False) -> dict:
    """
    单特征分箱。返回：
      {'table': [...], 'iv': float, 'method': str, 'max_bins': int,
       'assign': {'kind': 'numeric'|'category', 'edges'|'groups', 'keys', 'woes'}}
    """
    data = pd.DataFrame({'x': s, 'y': y}).copy()
    data['y'] = data['y'].astype(int)
    n = len(data)
    total_bad = int(data['y'].sum())
    total_good = n - total_bad
    if total_good == 0 or total_bad == 0:
        raise ValueError('目标列只有一个类别，无法分箱')

    is_numeric = pd.api.types.is_numeric_dtype(data['x'])
    has_missing = bool(data['x'].isna().any())
    nonnull = data.dropna(subset=['x'])
    missing_df = data[data['x'].isna()] if has_missing else None

    if not is_numeric or nonnull['x'].nunique() <= max_bins:
        # 离散/低基数：按取值分组
        grouped = nonnull.groupby('x', sort=True)['y'].agg(['sum', 'count']).reset_index()
        grouped.columns = ['val', 'bad', 'count']
        grouped['good'] = grouped['count'] - grouped['bad']
        assign = {'kind': 'category',
                  'items': [{'label': str(v), 'good': int(g), 'bad': int(b)}
                            for v, g, b in zip(grouped['val'], grouped['good'], grouped['bad'])]}
    else:
        x = nonnull['x']
        if method == 'equal_width':
            edges = np.linspace(x.min(), x.max(), min(max_bins, x.nunique()) + 1)
            iv_series = pd.cut(x, bins=edges, right=False, include_lowest=True, duplicates='drop')
        elif method == 'quantile':
            iv_series = pd.qcut(x, q=max_bins, duplicates='drop')
        else:  # chimerge
            init = pd.qcut(x, q=min(q_init, x.nunique()), duplicates='drop')
            grp = pd.DataFrame({'iv': init, 'y': nonnull['y']}) \
                .groupby('iv', observed=True)['y'].agg(['sum', 'count']).reset_index()
            grp.columns = ['iv', 'bad', 'count']
            grp['good'] = grp['count'] - grp['bad']
            grp['left'] = grp['iv'].apply(lambda iv: iv.left)
            bins_df = _chi2_merge(grp, max_bins)
            # 用合并后的 Interval 重新标记样本
            iv_series = pd.cut(x, bins=sorted(set(
                [b.left for b in bins_df['iv']] + [b.right for b in bins_df['iv']])),
                right=False, include_lowest=True)

        grouped = pd.DataFrame({'iv': iv_series, 'y': nonnull['y']}) \
            .groupby('iv', observed=True)['y'].agg(['sum', 'count']).reset_index()
        grouped.columns = ['iv', 'bad', 'count']
        grouped['good'] = grouped['count'] - grouped['bad']
        grouped = grouped.sort_values('iv').reset_index(drop=True)

        edges = sorted(set([iv.left for iv in grouped['iv']] + [iv.right for iv in grouped['iv']]))
        assign = {'kind': 'numeric', 'edges': [round(float(e), 6) for e in edges],
                  'items': [{'label': f"[{iv.left:.4g}, {iv.right:.4g}]",
                             'left': float(iv.left), 'right': float(iv.right),
                             'good': int(g), 'bad': int(b)}
                            for iv, g, b in zip(grouped['iv'], grouped['good'], grouped['bad'])]}

    # 单调性约束：合并破坏单调的相邻箱（仅数值特征）
    if monotonic and assign['kind'] == 'numeric' and len(assign['items']) > 2:
        assign['items'] = _merge_to_monotonic(assign['items'])
        # 合并后同步重建 edges（保持与 items 一致，供 woe_assign 使用）
        _es = set()
        for _it in assign['items']:
            if _it.get('left') is not None:
                _es.add(_it['left'])
            if _it.get('right') is not None:
                _es.add(_it['right'])
        assign['edges'] = [round(float(e), 6) for e in sorted(_es)]

    # 缺失样本作为附加箱
    if missing_df is not None and len(missing_df) > 0:
        mb = int(missing_df['y'].sum())
        mg = len(missing_df) - mb
        assign['items'].append({'label': '缺失', 'good': int(mg), 'bad': int(mb)})

    rows = _make_table(assign, total_good, total_bad)
    return {'table': rows, 'iv': assign['iv_total'],
            'method': method, 'max_bins': max_bins, 'assign': {
                'kind': assign['kind'],
                'edges': assign.get('edges'),
                'groups': assign.get('groups'),
                'keys': assign['keys'],
                'woes': assign['woes'],
            }}


def woe_assign(s: pd.Series, info: dict) -> pd.Series:
    """按分箱 assign 把特征映射为 WOE 值（训练与打分共用，保证一致）"""
    assign = info['assign']
    s = pd.Series(s).copy()
    out = pd.Series(np.nan, index=s.index)
    keys, woes = assign['keys'], assign['woes']
    woe_map = dict(zip(keys, woes))

    if assign['kind'] == 'numeric':
        edges = assign['edges']
        labels = list(range(len(edges) - 1))
        cut = pd.cut(s, bins=edges, right=False, include_lowest=True, labels=labels,
                     duplicates='drop')
        for lab in labels:
            mask = cut == lab
            if mask.any():
                out[mask] = woes[lab]
    else:
        str_vals = s.astype(str)
        for k, w in woe_map.items():
            out[str_vals == k] = w

    # 缺失箱或未命中 → 缺失箱 woe（若有）否则 0
    miss_key = '缺失'
    if miss_key in woe_map and out.isna().any():
        out[out.isna()] = woe_map[miss_key]
    out = out.fillna(0.0)
    return out.astype(float)


# ---------------- 建模与评分 ----------------

def build_scorecard(df: pd.DataFrame, target_col: str, feature_cols: list,
                    method: str = 'chimerge', max_bins: int = 5, min_iv: float = 0.02,
                    base_score: int = 600, pdo: int = 50, base_odds: float = 50.0,
                    test_size: float = 0.3, seed: int = 42,
                    monotonic: bool = False, reject_infer: bool = False) -> dict:
    """
    完整评分卡流程：
    1) 每个特征分箱 + WOE/IV，筛选 IV >= min_iv（可单调性约束）
    2) WOE 替换特征，逻辑回归（训练/测试）
    3) 评分刻度映射 A/B
    4) 评估：AUC / KS / Gini / 混淆矩阵 / 好坏客户均分
    reject_infer=True：目标含缺失（未观察样本）时，先基于已标签样本训练初模，
                       用预测概率对缺失样本打标（>0.5 违约），再重训（启发式拒绝推断）
    """
    from sklearn.model_selection import train_test_split
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score, roc_curve, confusion_matrix

    y_raw = pd.to_numeric(df[target_col], errors='coerce')
    if reject_infer and y_raw.isna().any():
        lab = y_raw.notna()
        if int(lab.sum()) >= 10:
            y_lab = y_raw[lab].astype(int)
            temp_info = {}
            for col in feature_cols:
                if col in df.columns and col != target_col:
                    try:
                        temp_info[col] = bin_series(df.loc[lab, col], y_lab,
                                                    method=method, max_bins=max_bins)
                    except Exception:
                        pass
            if temp_info:
                Xt = pd.DataFrame(index=df.index)
                for col, info in temp_info.items():
                    Xt[col] = woe_assign(df[col], info)
                Xt = Xt.replace([np.inf, -np.inf], np.nan).fillna(0.0)
                lr0 = LogisticRegression(max_iter=1000)
                lr0.fit(Xt.loc[lab], y_lab)
                proba = lr0.predict_proba(Xt)[:, 1]
                y_raw = y_raw.copy()
                y_raw[y_raw.isna()] = (proba[y_raw.isna()] > 0.5).astype(int)
    y = y_raw.astype(int)

    woe_info = {}
    for col in feature_cols:
        if col not in df.columns or col == target_col:
            continue
        try:
            res = bin_series(df[col], y, method=method, max_bins=max_bins, monotonic=monotonic)
        except Exception:
            continue
        if res['iv'] is None or math.isnan(res['iv']):
            continue
        woe_info[col] = res

    kept = {c: v for c, v in woe_info.items() if v['iv'] >= min_iv}
    if not kept:
        return {'ok': False, 'error': '没有特征通过 IV >= %.2f 筛选（请降低 min_iv 或增加特征）' % min_iv}

    X_woe = pd.DataFrame(index=df.index)
    for col, info in kept.items():
        X_woe[col] = woe_assign(df[col], info)
    X_woe = X_woe.replace([np.inf, -np.inf], np.nan).fillna(0.0)

    X_train, X_test, y_train, y_test = train_test_split(
        X_woe, y, test_size=test_size, random_state=seed, stratify=y)

    model = LogisticRegression(C=1.0, max_iter=1000, solver='lbfgs')
    model.fit(X_train, y_train)

    B = pdo / math.log(2)
    A = base_score + B * math.log(base_odds)
    intercept = float(model.intercept_[0])
    coefs = {c: float(model.coef_[0][i]) for i, c in enumerate(X_woe.columns)}

    def _score(df_woe):
        logit = intercept + sum(coefs[c] * df_woe[c] for c in coefs)
        return A - B * logit

    train_score = _score(X_train)
    test_score = _score(X_test)
    # 评估用模型违约概率（分数高=好客户，与概率方向相反）
    train_proba = model.predict_proba(X_train)[:, 1]
    test_proba = model.predict_proba(X_test)[:, 1]
    train_auc = roc_auc_score(y_train, train_proba)
    test_auc = roc_auc_score(y_test, test_proba)

    fpr, tpr, _ = roc_curve(y_test, test_proba)
    ks = float(max(tpr - fpr))

    thr = float(test_score.median())
    pred_good = (test_score >= thr).astype(int)
    cm = confusion_matrix(y_test, pred_good)

    y_test_s = pd.Series(y_test.values, index=test_score.index)
    avg_good = float(test_score[y_test_s == 0].mean()) if (y_test_s == 0).any() else None
    avg_bad = float(test_score[y_test_s == 1].mean()) if (y_test_s == 1).any() else None

    return {
        'ok': True,
        'method': method,
        'max_bins': max_bins,
        'min_iv': min_iv,
        'n_features_total': len(woe_info),
        'n_features_kept': len(kept),
        'kept_features': list(kept.keys()),
        'woe_info': {c: {'iv': v['iv'], 'table': v['table'], 'assign': v['assign']}
                     for c, v in kept.items()},
        'model': {
            'coefs': coefs,
            'intercept': round(intercept, 6),
            'A': round(A, 4),
            'B': round(B, 4),
            'base_score': base_score,
            'pdo': pdo,
            'base_odds': base_odds,
        },
        'eval': {
            'train_auc': round(train_auc, 4), 'test_auc': round(test_auc, 4),
            'ks': round(ks, 4), 'gini': round(2 * round(test_auc, 4) - 1, 4),  # 用取整 AUC，保证可人工验算
            'confusion_matrix': cm.tolist(),
            'avg_score_good': round(avg_good, 2) if avg_good is not None else None,
            'avg_score_bad': round(avg_bad, 2) if avg_bad is not None else None,
            'n_train': int(len(X_train)),
            'n_test': int(len(X_test)),
        },
        'params': {'base_score': base_score, 'pdo': pdo, 'base_odds': base_odds},
    }


def apply_scorecard(df: pd.DataFrame, result: dict) -> pd.DataFrame:
    """用评分卡结果对新数据集打分，返回带 score / risk_level 列的副本"""
    kept = result.get('kept_features', [])
    woe_info = result.get('woe_info', {})
    model = result.get('model', {})
    if not kept or not model:
        raise ValueError('评分卡结果无效（缺少特征或模型参数）')

    X_woe = pd.DataFrame(index=df.index)
    for col in kept:
        X_woe[col] = woe_assign(df[col], woe_info[col])
    X_woe = X_woe.replace([np.inf, -np.inf], np.nan).fillna(0.0)

    intercept = float(model['intercept'])
    coefs = model['coefs']
    A, B = float(model['A']), float(model['B'])
    logit = intercept + sum(coefs[c] * X_woe[c] for c in coefs)
    score = A - B * logit

    out = df.copy()
    out['score'] = score.round(1)
    q = out['score'].quantile([0.2, 0.4, 0.6, 0.8])

    def _level(x):
        if x <= q[0.2]: return '高风险'
        if x <= q[0.4]: return '中高风险'
        if x <= q[0.6]: return '中风险'
        if x <= q[0.8]: return '中低风险'
        return '低风险'
    out['risk_level'] = out['score'].apply(_level)
    return out


def psi_score(expected_scores: pd.Series, actual_scores: pd.Series, bins: int = 10) -> float:
    """PSI 群体稳定性：按期望分布分位切分后比较两个分布的占比差异"""
    expected = expected_scores.dropna()
    actual = actual_scores.dropna()
    if len(expected) == 0 or len(actual) == 0:
        return float('nan')
    edges = pd.qcut(expected, q=bins, duplicates='drop')
    exp_dist = edges.value_counts(normalize=True).sort_index()
    act_dist = pd.cut(actual, bins=edges.cat.categories).value_counts(normalize=True).sort_index()
    psi = 0.0
    for cat in exp_dist.index:
        e = max(float(exp_dist.get(cat, 0.0)), 1e-6)
        a = max(float(act_dist.get(cat, 0.0)), 1e-6)
        psi += (a - e) * math.log(a / e)
    return round(float(psi), 4)


# ---------------- 模型持久化与报告 ----------------

def _safe_name(name: str) -> str:
    safe = "".join(c for c in str(name) if c.isalnum() or c in '._- ').strip()
    return safe or 'model'


def save_scorecard(result: dict, name: str = 'model') -> str:
    """保存评分卡结果到 output/models/，返回文件名"""
    os.makedirs(MODEL_DIR, exist_ok=True)
    fname = f"scorecard_{_safe_name(name)}.json"
    with open(os.path.join(MODEL_DIR, fname), 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return fname


def list_scorecards() -> list:
    """列出已保存的评分卡模型"""
    os.makedirs(MODEL_DIR, exist_ok=True)
    files = []
    for fn in sorted(os.listdir(MODEL_DIR)):
        if not fn.endswith('.json'):
            continue
        p = os.path.join(MODEL_DIR, fn)
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(p)).strftime('%Y-%m-%d %H:%M')
        except Exception:
            mtime = ''
        files.append({'name': fn, 'size': os.path.getsize(p), 'mtime': mtime})
    return files


def load_scorecard(name: str) -> dict:
    """按文件名载入评分卡"""
    fn = _safe_name(name)
    if not fn.endswith('.json'):
        fn += '.json'
    path = os.path.join(MODEL_DIR, fn)
    if not os.path.exists(path):
        raise FileNotFoundError(f'模型不存在: {fn}')
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def generate_pdf_report(result: dict, out_path: str) -> str:
    """用 reportlab 生成评分卡 PDF 报告，返回文件路径"""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle)
    from reportlab.lib.units import mm

    styles = getSampleStyleSheet()
    title_s = ParagraphStyle('T', parent=styles['Title'], fontSize=16, spaceAfter=8)
    h_s = ParagraphStyle('H', parent=styles['Heading2'], fontSize=12, spaceBefore=10, spaceAfter=4)
    body_s = ParagraphStyle('B', parent=styles['BodyText'], fontSize=9, leading=13)

    story = [Paragraph('信用评分卡模型报告', title_s),
             Paragraph(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}　|　分箱方法：{result.get('method')}　|　最大箱数：{result.get('max_bins')}", body_s),
             Spacer(1, 6)]

    # 评估指标
    ev = result.get('eval', {})
    m = result.get('model', {})
    story.append(Paragraph('一、模型评估', h_s))
    metrics = [
        ['指标', '数值'],
        ['测试集 AUC', str(ev.get('test_auc', ''))],
        ['KS 统计量', str(ev.get('ks', ''))],
        ['Gini 系数', str(ev.get('gini', ''))],
        ['训练集 AUC', str(ev.get('train_auc', ''))],
        ['好客户均分', str(ev.get('avg_score_good', ''))],
        ['坏客户均分', str(ev.get('avg_score_bad', ''))],
        ['样本量(训练/测试)', f"{ev.get('n_train', '')} / {ev.get('n_test', '')}"],
    ]
    t = Table(metrics, colWidths=[90*mm, 90*mm])
    t.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f0f2f5')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(t)

    # 评分刻度
    story.append(Paragraph('二、评分刻度', h_s))
    story.append(Paragraph(
        f"Score = {m.get('A', '')} − {m.get('B', '')} × ln(odds)　（基准分 {m.get('base_score')}，PDO {m.get('pdo')}，基准odds {m.get('base_odds')}）", body_s))
    coefs = m.get('coefs', {})
    story.append(Paragraph('入选变量及系数：' + '，'.join(f"{c}={coefs[c]:.4f}" for c in coefs) + f"　截距={m.get('intercept')}", body_s))

    # 各变量 WOE 表
    story.append(Paragraph('三、变量分箱 / WOE / IV', h_s))
    for c in result.get('kept_features', []):
        info = result['woe_info'][c]
        story.append(Paragraph(f"▍{c}　（IV = {info['iv']}）", body_s))
        data = [['分箱', '样本数', '坏数', '坏率', 'WOE', 'IV']]
        for r in info['table']:
            data.append([r['bin'], str(r['count']), str(r['bad']),
                         str(r['bad_rate']), str(r['woe']), str(r['iv'])])
        t2 = Table(data, colWidths=[45*mm, 25*mm, 20*mm, 20*mm, 30*mm, 30*mm])
        t2.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f0f2f5')),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(t2)
        story.append(Spacer(1, 6))

    doc = SimpleDocTemplate(out_path, pagesize=A4,
                            rightMargin=15*mm, leftMargin=15*mm,
                            topMargin=15*mm, bottomMargin=15*mm)
    doc.build(story)
    return out_path
