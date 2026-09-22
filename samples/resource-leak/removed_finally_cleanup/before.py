def process(conn):
    try:
        conn.begin()
        conn.run("UPDATE t SET x = 1")
    finally:
        conn.close()
