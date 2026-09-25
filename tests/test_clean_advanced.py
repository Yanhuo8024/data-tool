# -*- coding: utf-8 -*-
"""数据清洗增强模块的回归测试。"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clean_advanced import standardize_dates


def test_standardize_dates_marks_only_invalid_rows_when_preserved():
    df = pd.DataFrame({"date": ["2026-09-10", "bad-date", "2026-09-11"]})

    cleaned, result = standardize_dates(df, "date", remove_invalid=False)

    assert cleaned["date"].tolist() == ["2026-09-10", "Invalid", "2026-09-11"]
    assert result["invalid_removed"] == 1
