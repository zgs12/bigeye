#!/usr/bin/env python3
"""给 messages 表的 user 消息建向量索引（一次性 backfill + 增量钩子）。
表: user_msg_vectors(message_id PK, vec BLOB, ts REAL)
"""
import sqlite3, os, sys, time, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8')

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, 'data', 'chat.db')

def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_msg_vectors (
            message_id INTEGER PRIMARY KEY,
            vec BLOB NOT NULL,
            ts REAL
        )
    """)

def vec_to_blob(v):
    return struct.pack(f'{len(v)}f', *v)

def main():
    from memory.embedder import embed
    conn = sqlite3.connect(DB)
    ensure_table(conn)
    # 已有向量数
    done = conn.execute("SELECT COUNT(*) FROM user_msg_vectors").fetchone()[0]
    # 全部 user 消息
    rows = conn.execute("SELECT id, text, ts FROM messages WHERE role='user' ORDER BY id").fetchall()
    print(f'user 消息总数: {len(rows)}, 已有向量: {done}')
    # 预热 embedder
    embed('预热')
    n = 0
    for mid, text, ts in rows:
        if not text or len(text.strip()) < 2:
            continue
        v = embed(text[:300])
        if not v:
            continue
        conn.execute("INSERT OR REPLACE INTO user_msg_vectors(message_id, vec, ts) VALUES(?,?,?)",
                     (mid, vec_to_blob(v), ts))
        n += 1
        if n % 200 == 0:
            conn.commit()
            print(f'  ...{n}/{len(rows)}')
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM user_msg_vectors").fetchone()[0]
    print(f'完成: 新写入 {n}, 表内总计 {total}')
    conn.close()

if __name__ == '__main__':
    main()
