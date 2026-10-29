"""Sanitized routing counters, so a live GUI can be checked without payload logs."""
import json
import os
from pathlib import Path
import threading
import time

from claude_swap.fsutil import atomic_write_text


class State:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {'pid': os.getpid(), 'running': True, 'startedAt': time.time(),
                     'routedRequests': 0, 'unchangedRequests': 0, 'lastAccount': None}
        self.publish()

    def publish(self):
        atomic_write_text(self.path, json.dumps(self.data))

    def record(self, identity):
        with self.lock:
            key = 'routedRequests' if identity is not None else 'unchangedRequests'
            self.data[key] += 1
            if identity is not None:
                self.data['lastAccount'] = identity.account_uuid[:8]
            self.data['updatedAt'] = time.time()
            try:
                self.publish()
            except OSError:
                pass  # counters must never abort an accepted model request

    def close(self):
        with self.lock:
            self.data['running'] = False
            self.publish()
