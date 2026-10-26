"""QA 用的测试顺序打乱插件（只影响收集顺序，不改任何用例）。

背景：`pyproject.toml` 里有 `addopts = "-q --strict-markers"`，
但环境里**没有安装 `pytest-randomly`**，所以 `-p no:randomly` 是个空操作 ——
"换个顺序跑一次"这件事从来没有真正发生过。这里补上。

用法：
    pytest -p tests.qa_shuffle_plugin -q                   # 默认 seed=271828
    QA_SHUFFLE_SEED=42 pytest -p tests.qa_shuffle_plugin -q
"""

from __future__ import annotations

import os
import random


def pytest_collection_modifyitems(session, config, items):  # noqa: ANN001, ARG001
    seed = int(os.environ.get("QA_SHUFFLE_SEED", "271828"))
    rng = random.Random(seed)
    rng.shuffle(items)
    config.stash[_SEED_KEY] = seed


_SEED_KEY = "qa_shuffle_seed"


def pytest_report_header(config):  # noqa: ANN001
    seed = os.environ.get("QA_SHUFFLE_SEED", "271828")
    return f"qa-shuffle: 测试顺序已按 seed={seed} 打乱"
