"""Start the local server, verify readiness, then open its UI."""
import ctypes
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
import urllib.request
import webbrowser

ROOT = Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8765'


def ready():
    try:
        with urllib.request.urlopen(URL + '/api/state', timeout=1) as response:
            state = json.load(response)
        return isinstance(state.get('settings'), dict) and 'vacancies' in state
    except Exception:
        return False


def main():
    data = ROOT / 'data'
    data.mkdir(exist_ok=True)
    try:
        if not ready():
            interpreter = Path(sys.executable)
            windowless = interpreter.with_name('pythonw.exe')
            if not getattr(sys,'frozen',False) and sys.platform == 'win32' and windowless.exists():
                interpreter = windowless
            command=([str(interpreter),'--serve','--no-open'] if getattr(sys,'frozen',False)
                     else [str(interpreter),'-u',str(ROOT/'app.py'),'--no-open'])
            flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == 'win32' else 0
            with (data / 'server.log').open('ab') as log:
                process = subprocess.Popen(
                    command, cwd=ROOT,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=flags, start_new_session=sys.platform != 'win32')
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if ready():
                    break
                if process.poll() is not None:
                    raise RuntimeError('Сервер завершился. Подробности: ' + str(data / 'server.log'))
                time.sleep(.25)
            else:
                raise RuntimeError('Сервер не ответил за 15 секунд. Журнал: ' + str(data / 'server.log'))
        if '--no-open' not in sys.argv:
            webbrowser.open(URL)
        return 0
    except Exception as error:
        (data / 'launch-error.log').write_text(traceback.format_exc(), encoding='utf-8')
        if sys.platform == 'win32':
            ctypes.windll.user32.MessageBoxW(None, str(error), 'Помощник hh.ru — ошибка запуска', 0x10)
        else:
            print(error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    if '--serve' in sys.argv:
        sys.argv.remove('--serve')
        import app
        app.main()
        raise SystemExit(0)
    raise SystemExit(main())
