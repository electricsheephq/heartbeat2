"""Configurable JSON command transport, with no implicit account selection."""
from datetime import datetime
import json
import subprocess


class ClientError(RuntimeError):
    pass


class CommandClient:
    def __init__(self, argv_prefix: list[str], peer: str):
        self.argv_prefix, self.peer = list(argv_prefix), peer

    def _run(self, args, single=False):
        try:
            result = subprocess.run(self.argv_prefix + args, capture_output=True, text=True, timeout=60)
            if result.returncode: raise ClientError(f'client exit {result.returncode}')
            rows = [json.loads(result.stdout)] if single else [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            for row in rows:
                if not isinstance(row, dict) or not {'id', 'date', 'edit_date', 'out', 'text'} <= row.keys():
                    raise ValueError('missing message fields')
                if not isinstance(row['id'], int) or not isinstance(row['out'], bool) or not isinstance(row['text'], str):
                    raise ValueError('invalid message fields')
                for key in ('date', 'edit_date'):
                    if key == 'date' or row[key] is not None:
                        if datetime.fromisoformat(row[key]).tzinfo is None: raise ValueError('date lacks timezone')
            return rows[0] if single else rows
        except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
            raise ClientError(f'client failure: {type(exc).__name__}') from exc

    def send(self, text):
        return self._run(['send', self.peer, text], single=True)

    def read(self, after_id: int | None, limit: int):
        args = ['read', self.peer, '--limit', str(limit)]
        if after_id is not None: args += ['--after-id', str(after_id)]
        return self._run(args)
