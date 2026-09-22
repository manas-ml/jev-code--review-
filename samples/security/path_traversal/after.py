import os

SAFE_ROOT = "/srv/uploads"

def read_upload(filename):
    path = os.path.join(SAFE_ROOT, filename)
    with open(path, "rb") as f:
        return f.read()
