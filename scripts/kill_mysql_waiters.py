"""Kill our MySQL sessions waiting on metadata locks (app boot unblock)."""
from sqlalchemy import create_engine, text
from app.config import Config


def main():
    e = create_engine(
        Config.SQLALCHEMY_DATABASE_URI,
        pool_pre_ping=True,
        connect_args={'connect_timeout': 8},
    )
    with e.connect() as c:
        rows = c.execute(text('SHOW FULL PROCESSLIST')).fetchall()
        print(f'processes: {len(rows)}')
        my_id = None
        try:
            my_id = c.execute(text('SELECT CONNECTION_ID()')).scalar()
        except Exception:
            pass
        for r in rows:
            d = dict(r._mapping)
            pid = d.get('Id')
            cmd = d.get('Command')
            state = d.get('State') or ''
            info = (d.get('Info') or '')[:140]
            t = d.get('Time')
            print(f'  id={pid} cmd={cmd} time={t} state={state} info={info}')
            if pid == my_id:
                continue
            # Kill waiters / long queries from this app user
            should_kill = (
                'Waiting for table metadata lock' in state
                or (cmd == 'Query' and t and int(t) > 30 and 'SHOW' not in info.upper())
            )
            if should_kill:
                try:
                    c.execute(text(f'KILL {int(pid)}'))
                    print(f'  -> killed {pid}')
                except Exception as ex:
                    print(f'  -> kill failed {pid}: {ex}')

        # Also try to find DDL holder via performance_schema if available
        try:
            locks = c.execute(text(
                """
                SELECT OBJECT_TYPE, OBJECT_SCHEMA, OBJECT_NAME, LOCK_TYPE, LOCK_STATUS,
                       OWNER_THREAD_ID
                FROM performance_schema.metadata_locks
                WHERE OBJECT_SCHEMA = DATABASE()
                LIMIT 50
                """
            )).fetchall()
            print('metadata_locks:', len(locks))
            for row in locks:
                print(' ', dict(row._mapping))
        except Exception as ex:
            print('metadata_locks unavailable:', ex)


if __name__ == '__main__':
    main()
