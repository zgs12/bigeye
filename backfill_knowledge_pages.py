"""知识页存量迁移 backfill：把已有 observation 归组进知识页。

用法：py backfill_knowledge_pages.py [limit]
默认全量 827 条。传 limit 只处理前 N 条（按 id ASC，存量未归组）。
"""
import sys, os, json, time
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

limit = int(sys.argv[1]) if len(sys.argv) > 1 else None

from memory.reflection_loop import _default_llm_call
from memory.knowledge_pages import KnowledgePageStore, _get_db_path

DEFAULT_DB_PATH = _get_db_path()

store = KnowledgePageStore(db_path=DEFAULT_DB_PATH, llm_call_fn=_default_llm_call)

t0 = time.time()
report = store.backfill(limit=limit)
dt = time.time() - t0

print(f"[backfill] limit={limit} 耗时 {dt:.1f}s")
print(f"[backfill] {json.dumps(report, ensure_ascii=False)}")

# 汇总现状
import sqlite3
conn = sqlite3.connect(DEFAULT_DB_PATH)
conn.row_factory = sqlite3.Row
pages = conn.execute("SELECT id, page_key, current_version, obs_ids FROM knowledge_pages").fetchall()
print(f"[backfill] 当前页数: {len(pages)}")
ver_cnt = conn.execute("SELECT COUNT(*) c FROM knowledge_page_versions").fetchone()['c']
print(f"[backfill] 成文版本数: {ver_cnt}")
for p in pages[:15]:
    obs_n = len(json.loads(p['obs_ids']) if p['obs_ids'] else [])
    print(f"  page#{p['id']} v{p['current_version']} obs={obs_n} key={p['page_key'][:24]}")
conn.close()
