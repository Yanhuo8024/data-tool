"""
routes/scorecard_routes.py — 信用评分卡 API
"""
from datetime import datetime
from flask import Blueprint, request, jsonify
import traceback

from core_config import OUT_DIR, logger, pd
from auth import local_guard

scorecard_bp = Blueprint('scorecard', __name__)
# 分析接口消耗本地算力：远程请求必须认证，本地/LAN 匿名回退 admin
scorecard_bp.before_request(local_guard)


def _load_df(data):
    from core_config import _get_csv_file
    filepath = _get_csv_file(data)
    if not filepath:
        return None, '请先选择数据文件'
    try:
        df = pd.read_csv(filepath, encoding='utf-8-sig')
    except Exception:
        df = pd.read_csv(filepath, encoding='utf-8')
    if df is None or df.empty:
        return None, '文件为空'
    return df, None


@scorecard_bp.route('/api/scorecard/columns', methods=['POST'])
def api_scorecard_columns():
    """列出文件列 + 识别 0/1 二分类候选目标列"""
    data = request.get_json() or {}
    df, err = _load_df(data)
    if err:
        return jsonify({'error': err}), 400
    cols = []
    for c in df.columns:
        nunique = int(df[c].nunique(dropna=True))
        is_numeric = pd.api.types.is_numeric_dtype(df[c])
        candidate_target = is_numeric and nunique <= 2 and nunique >= 1
        cols.append({
            'name': str(c),
            'dtype': str(df[c].dtype),
            'nunique': nunique,
            'missing': int(df[c].isna().sum()),
            'candidate_target': bool(candidate_target),
        })
    return jsonify({'ok': True, 'columns': cols})


@scorecard_bp.route('/api/scorecard/binning', methods=['POST'])
def api_scorecard_binning():
    """单特征分箱 + WOE/IV 表（预览）"""
    data = request.get_json() or {}
    df, err = _load_df(data)
    if err:
        return jsonify({'error': err}), 400
    target = (data.get('target_col') or '').strip()
    feature = (data.get('feature') or '').strip()
    if target not in df.columns or feature not in df.columns:
        return jsonify({'error': '目标列或特征列不存在'}), 400
    y = df[target].astype(int)
    from scorecard import bin_series
    try:
        res = bin_series(df[feature], y,
                         method=data.get('method', 'chimerge'),
                         max_bins=int(data.get('max_bins', 5)))
    except Exception as e:
        return jsonify({'error': f'分箱失败: {e}'}), 500
    return jsonify({'ok': True, 'feature': feature, 'iv': res['iv'], 'table': res['table']})


@scorecard_bp.route('/api/scorecard/train', methods=['POST'])
def api_scorecard_train():
    """训练评分卡：分箱 → WOE/IV → 逻辑回归 → 评分刻度 → 评估"""
    data = request.get_json() or {}
    df, err = _load_df(data)
    if err:
        return jsonify({'error': err}), 400
    target = (data.get('target_col') or '').strip()
    features = data.get('feature_cols') or []
    if target not in df.columns:
        return jsonify({'error': f'目标列不存在: {target}'}), 400
    features = [c for c in features if c in df.columns and c != target]
    if not features:
        return jsonify({'error': '未选择有效特征列'}), 400
    if df[target].nunique() != 2:
        return jsonify({'error': '目标列必须是 0/1 二分类（当前 %d 个取值）' % df[target].nunique()}), 400

    from scorecard import build_scorecard
    try:
        result = build_scorecard(
            df, target, features,
            method=data.get('method', 'chimerge'),
            max_bins=int(data.get('max_bins', 5)),
            min_iv=float(data.get('min_iv', 0.02)),
            base_score=int(data.get('base_score', 600)),
            pdo=int(data.get('pdo', 50)),
            base_odds=float(data.get('base_odds', 50)),
            monotonic=bool(data.get('monotonic', False)),
            reject_infer=bool(data.get('reject_infer', False)),
        )
    except Exception as e:
        logger.error("scorecard train 失败: %s\n%s", e, traceback.format_exc())
        return jsonify({'error': f'训练失败: {e}'}), 500
    if not result.get('ok'):
        return jsonify({'error': result.get('error', '训练失败')}), 400
    result['file'] = data.get('file', '')
    result['target_col'] = target
    result['params']['monotonic'] = bool(data.get('monotonic', False))
    result['params']['reject_infer'] = bool(data.get('reject_infer', False))
    return jsonify(result)


@scorecard_bp.route('/api/scorecard/save', methods=['POST'])
def api_scorecard_save():
    """保存评分卡模型到 output/models/"""
    data = request.get_json() or {}
    result = data.get('result') or {}
    if not result or not result.get('model'):
        return jsonify({'error': '缺少评分卡结果（请先训练）'}), 400
    name = (data.get('name') or 'model').strip()
    from scorecard import save_scorecard
    try:
        fname = save_scorecard(result, name)
    except Exception as e:
        return jsonify({'error': f'保存失败: {e}'}), 500
    return jsonify({'ok': True, 'file': fname})


@scorecard_bp.route('/api/scorecard/models', methods=['GET'])
def api_scorecard_models():
    """列出已保存模型"""
    from scorecard import list_scorecards
    return jsonify({'ok': True, 'models': list_scorecards()})


@scorecard_bp.route('/api/scorecard/load', methods=['POST'])
def api_scorecard_load():
    """载入已保存模型"""
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': '缺少模型名'}), 400
    from scorecard import load_scorecard
    try:
        result = load_scorecard(name)
    except Exception as e:
        return jsonify({'error': f'载入失败: {e}'}), 404
    return jsonify({'ok': True, **result})


@scorecard_bp.route('/api/scorecard/report', methods=['POST'])
def api_scorecard_report():
    """导出评分卡 PDF 报告到 output/"""
    data = request.get_json() or {}
    result = data.get('result') or {}
    if not result or not result.get('model'):
        return jsonify({'error': '缺少评分卡结果（请先训练）'}), 400
    from scorecard import generate_pdf_report
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    fname = f"scorecard_report_{stamp}.pdf"
    try:
        generate_pdf_report(result, str(OUT_DIR / fname))
    except Exception as e:
        return jsonify({'error': f'报告生成失败: {e}'}), 500
    return jsonify({'ok': True, 'file': fname})


@scorecard_bp.route('/api/scorecard/predict', methods=['POST'])
def api_scorecard_predict():
    """应用评分卡：对新数据集打分，保存 CSV 到 output/"""
    data = request.get_json() or {}
    df, err = _load_df(data)
    if err:
        return jsonify({'error': err}), 400
    result = data.get('result') or {}
    if not result or not result.get('model'):
        return jsonify({'error': '缺少评分卡结果（请先训练）'}), 400

    from scorecard import apply_scorecard
    try:
        out = apply_scorecard(df, result)
    except Exception as e:
        return jsonify({'error': f'打分失败: {e}'}), 500

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    fname = f"scorecard_{stamp}.csv"
    out.to_csv(OUT_DIR / fname, index=False, encoding='utf-8-sig')

    preview = out[['score', 'risk_level']].head(10).fillna('').astype(str).to_dict(orient='records')
    dist = out['risk_level'].value_counts().to_dict()
    return jsonify({
        'ok': True,
        'file': fname,
        'rows': len(out),
        'score_min': round(float(out['score'].min()), 1),
        'score_max': round(float(out['score'].max()), 1),
        'score_mean': round(float(out['score'].mean()), 1),
        'risk_dist': dist,
        'preview': preview,
    })


@scorecard_bp.route('/api/scorecard/psi', methods=['POST'])
def api_scorecard_psi():
    """PSI 稳定性：训练集 vs 当前批次 同模型打分分布比较"""
    data = request.get_json() or {}
    train_file = (data.get('train_file') or '').strip()
    current_file = (data.get('current_file') or '').strip()
    result = data.get('result') or {}
    if not train_file or not current_file:
        return jsonify({'error': '请选择训练集文件与当前批次文件'}), 400
    if not result or not result.get('model'):
        return jsonify({'error': '缺少评分卡结果（请先训练）'}), 400

    from core_config import OUT_DIR
    from pathlib import Path
    try:
        train_df = pd.read_csv(OUT_DIR / train_file, encoding='utf-8-sig')
        current_df = pd.read_csv(OUT_DIR / current_file, encoding='utf-8-sig')
    except Exception as e:
        return jsonify({'error': f'读取文件失败: {e}'}), 400

    from scorecard import apply_scorecard, psi_score
    try:
        s_train = apply_scorecard(train_df, result)['score']
        s_current = apply_scorecard(current_df, result)['score']
        psi = psi_score(s_train, s_current, bins=10)
    except Exception as e:
        return jsonify({'error': f'PSI 计算失败: {e}'}), 500

    if psi is None or psi != psi:  # nan
        verdict = '无法计算'
    elif psi < 0.1:
        verdict = '稳定 ✅'
    elif psi < 0.25:
        verdict = '轻度偏移 ⚠️ 建议监控'
    else:
        verdict = '严重偏移 ❌ 需重新训练'
    return jsonify({'ok': True, 'psi': psi, 'verdict': verdict,
                    'train_file': train_file, 'current_file': current_file})
