"""Private atomic journals; never acknowledge submission before intent is durable."""
import json
import os
from pathlib import Path
import tempfile
import threading
from datetime import datetime, timezone

WRITE_LOCK = threading.RLock()


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')


def atomic_json(file, data):
    file = Path(file)
    file.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with WRITE_LOCK:
        fd, temp = tempfile.mkstemp(prefix=file.name+'.',suffix='.tmp',dir=file.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
                json.dump(data,stream,ensure_ascii=False,separators=(',',':'),allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp,file)
            if os.name != 'nt':
                fd = os.open(file.parent,os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
