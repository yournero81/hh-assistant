"""Cloud entry point: one authenticated HTTPS upstream, private desktop and app."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlparse


def nginx_config(port, origin):
    host=urlparse(origin)
    if host.scheme!='https' or not host.netloc or any(c in origin for c in '\n\r";{}'):
        raise ValueError('A valid HTTPS public origin is required')
    if not 1024<=port<=65535:
        raise ValueError('Invalid external port')
    return '''user www-data;
pid /tmp/hh-nginx.pid;
events { worker_connections 256; }
http {
  map $http_origin $hh_origin_allowed {
    default 0;
    "" 1;
    "ORIGIN_PLACEHOLDER" 1;
  }
  access_log off;
  error_log /dev/stderr warn;
  client_body_temp_path /tmp/client_temp;
  proxy_temp_path /tmp/proxy_temp;
  server {
    listen PORT_PLACEHOLDER;
    auth_basic "Personal assistant";
    auth_basic_user_file /tmp/hh-passwords;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;
    location = /healthz { auth_basic off; proxy_pass http://127.0.0.1:8765/healthz; }
    location /browser/ {
      if ($hh_origin_allowed = 0) { return 403; }
      proxy_pass http://127.0.0.1:6080/;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
      proxy_read_timeout 3600s;
      proxy_buffering off;
    }
    location = /browser/websockify {
      if ($http_origin != "ORIGIN_PLACEHOLDER") { return 403; }
      proxy_pass http://127.0.0.1:6080/websockify;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
      proxy_read_timeout 3600s;
      proxy_buffering off;
    }
    location / {
      proxy_pass http://127.0.0.1:8765;
      proxy_set_header Host $http_host;
      proxy_set_header Authorization $http_authorization;
      proxy_read_timeout 1200s;
    }
  }
}
'''.replace('PORT_PLACEHOLDER',str(port)).replace('ORIGIN_PLACEHOLDER',origin.rstrip('/'))


def main():
    password=os.environ.get('HH_ACCESS_PASSWORD','')
    if len(password)<24 or not os.environ.get('HH_DATABASE_URL') or len(os.environ.get('HH_MASTER_KEY',''))<32:
        raise RuntimeError('Set HH_ACCESS_PASSWORD, HH_DATABASE_URL and HH_MASTER_KEY before deploying')
    if not os.environ.get('HH_PROFILE_JSON'):
        raise RuntimeError('Set HH_PROFILE_JSON to the real applicant profile')
    origin=os.environ.get('HH_PUBLIC_ORIGIN') or os.environ.get('RENDER_EXTERNAL_URL','')
    Path('/tmp/hh-nginx.conf').write_text(nginx_config(int(os.environ.get('PORT','10000')),origin))
    subprocess.run(['htpasswd','-B','-i','-c','/tmp/hh-passwords','timur'],input=password+'\n',text=True,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    import grp
    os.chown('/tmp/hh-passwords',0,grp.getgrnam('www-data').gr_gid)
    os.chmod('/tmp/hh-passwords',0o640)
    env=os.environ.copy();env.update(DISPLAY=':99',HH_BIND_HOST='127.0.0.1',PORT='8765',HH_CLOUD_BROWSER='1',HH_ASSISTANT_HEADLESS='0')
    procs=[]
    def start(args):
        proc=subprocess.Popen(args,env=env);procs.append(proc);return proc
    def stop(*_):
        for proc in reversed(procs):
            if proc.poll() is None:proc.terminate()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)

    def display_ready():
        return subprocess.run(
            ['xdpyinfo','-display',':99'],env=env,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        ).returncode==0

    def clear_stale_display():
        """Remove :99 artefacts only when their recorded Xvfb is not usable."""
        lock=Path('/tmp/.X99-lock')
        if display_ready():
            return
        if lock.exists():
            try:
                pid=int(lock.read_text().strip())
                cmdline=Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0',b' ')
                if b'Xvfb' in cmdline and b':99' in cmdline:
                    os.kill(pid,signal.SIGTERM)
                    for _ in range(30):
                        if not Path(f'/proc/{pid}').exists():break
                        time.sleep(.1)
            except (OSError,ValueError):
                pass
        if not display_ready():
            lock.unlink(missing_ok=True)
            Path('/tmp/.X11-unix/X99').unlink(missing_ok=True)

    try:
        clear_stale_display()
        if not display_ready():
            xvfb=start(['Xvfb',':99','-screen','0','1200x850x24','-nolisten','tcp','-ac'])
            for _ in range(100):
                if display_ready():break
                if xvfb.poll() is not None:
                    raise RuntimeError('Xvfb exited before display :99 became ready')
                time.sleep(.1)
        if not display_ready():
            raise RuntimeError('Display :99 did not become ready')
        start(['fluxbox'])
        start(['x11vnc','-display',':99','-localhost','-rfbport','5900','-forever','-shared','-nopw','-quiet'])
        start(['websockify','--web=/usr/share/novnc','127.0.0.1:6080','127.0.0.1:5900'])
        start([sys.executable,'app.py','--no-open'])
        start(['nginx','-c','/tmp/hh-nginx.conf','-g','daemon off;'])
        while all(proc.poll() is None for proc in procs):time.sleep(1)
    finally:stop()
    return 1

if __name__=='__main__':sys.exit(main())
