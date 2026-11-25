"""Local alert sinks. Nothing here sends anything over a network: alerts are appended to a local JSON-lines file that
the user's own tooling (cron, a mail script, a desktop notifier) can read. A network sink would be a separate, explicit opt-in."""
import json, time

LEVELS = {'info': 0, 'warn': 1, 'critical': 2}


class FileNotifier:
    def __init__(self, path, min_level='warn'):
        if min_level not in LEVELS:
            raise ValueError('min level must be info, warn or critical')
        if '://' in str(path):
            raise ValueError('notify target must be a local file path, not a URL')
        self.path, self.min = path, LEVELS[min_level]

    def send(self, target, alerts):
        """alerts: [(level, code, msg)]. Returns how many were written."""
        rows = [{'ts': int(time.time()), 'level': l, 'code': c, 'msg': m, 'chain': target['chain'],
                 'address': target['address'], 'label': target.get('label', '')}
                for l, c, m in alerts if LEVELS.get(l, 0) >= self.min]
        if rows:
            with open(self.path, 'a', encoding='utf-8') as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + '\n')
        return len(rows)
