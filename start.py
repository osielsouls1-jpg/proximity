#!/usr/bin/env python3
"""
Proximity — Start Script
"""

import os, socket, subprocess, time, re, sys
from pathlib import Path

BACKEND = Path(__file__).parent / "backend"
PORT    = 8000

def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close()
        return ip
    except:
        return "localhost"

def start_tunnel():
    """Start localhost.run tunnel, return public URL (blocks until URL found or timeout)."""
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=30",
        "-o", "ExitOnForwardFailure=no",
        "-R", f"80:localhost:{PORT}",
        "nokey@localhost.run"
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    deadline = time.time() + 25
    for line in proc.stdout:
        if time.time() > deadline:
            break
        line = line.strip()
        m = re.search(r'https://[a-f0-9]+\.lhr\.life', line)
        if m:
            return m.group(0), proc

    return None, proc

def main():
    local_url = f"http://{local_ip()}:{PORT}"

    print("\n" + "═"*52)
    print("  📍  PROXIMITY")
    print("═"*52)
    print("  Starting tunnel...")

    public_url, tunnel_proc = start_tunnel()

    if public_url:
        (BACKEND / ".public_url").write_text(public_url)
        print(f"  Local  → {local_url}")
        print(f"  Public → \033[1;32m{public_url}\033[0m  ← share this!")
    else:
        print(f"  Local  → {local_url}")
        print(f"  Public → tunnel failed, running local only")

    print("═"*52)
    print("  Press Ctrl+C to stop\n")

    # Start FastAPI — use subprocess so tunnel stays alive
    os.chdir(BACKEND)
    server = subprocess.run([
        "python3", "-m", "uvicorn", "main:app",
        "--host", "0.0.0.0",
        "--port", str(PORT),
        "--reload"
    ])

if __name__ == "__main__":
    main()
