"""One anonymous browser subprocess per backend process, with bounded serial access."""
import atexit
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import os
import signal


class BrowserWorker:
    def __init__(self, *, timeout=75):
        self.timeout = timeout
        self.lock = threading.Lock()
        self.process = None
        self.responses = None
        atexit.register(self.close)

    def _start(self):
        self.process = subprocess.Popen(
            [sys.executable, '-u', '-m', 'app.agents.community_browser', '--serve'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', cwd=str(Path(__file__).resolve().parents[2]),
            start_new_session=os.name!='nt',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        responses = self.responses = queue.Queue(maxsize=2)
        stream = self.process.stdout
        def read():
            try:
                while True:
                    line = stream.readline(2_000_002)
                    if not line: break
                    if len(line.encode('utf-8')) > 2_000_000 or not line.endswith('\n'):
                        responses.put(None); return
                    responses.put(line)
            except (OSError, ValueError): pass
            finally:
                try: responses.put_nowait(None)
                except queue.Full: pass
        threading.Thread(target=read, name='community-browser-output', daemon=True).start()

    def close(self):
        process, self.process = self.process, None
        if process is None: return
        try:
            if process.stdin: process.stdin.close()  # EOF lets the worker close Playwright normally.
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            # Stop only this owned worker tree, including Chromium on hard timeouts.
            if os.name=='nt':
                try:
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],
                                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=5)
                except (OSError,subprocess.TimeoutExpired):
                    process.kill()
            else:
                try: os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError: pass
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=2)
        finally:
            if process.stdout: process.stdout.close()

    def search(self, keyword):
        if not isinstance(keyword, str) or not 1 <= len(keyword) <= 100:
            raise ValueError('Invalid keyword')
        if not self.lock.acquire(timeout=self.timeout):
            raise subprocess.TimeoutExpired('community-browser-queue', self.timeout)
        try:
            if self.process is None or self.process.poll() is not None:
                self.close(); self._start()
            try:
                self.process.stdin.write(json.dumps({'keyword': keyword}, ensure_ascii=True)+'\n')
                self.process.stdin.flush()
                line = self.responses.get(timeout=self.timeout)
                if line is None: raise ValueError('Browser worker stopped')
                result = json.loads(line)
                if not isinstance(result, dict): raise ValueError('Invalid browser worker response')
                return result
            except queue.Empty:
                self.close()
                raise subprocess.TimeoutExpired('community-browser', self.timeout) from None
            except (OSError, ValueError):
                self.close()
                raise ValueError('Browser worker unavailable') from None
        finally:
            self.lock.release()
