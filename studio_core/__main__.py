"""CLI:  python -m studio_core <command>"""
import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from . import brief, config, ingest, projects, providers, router, status, vision


def _exit_with_parent() -> None:
    """Exit when the launching window/process dies, so the port can never be left held."""
    ppid = os.getppid()
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x00100000, False, ppid)  # SYNCHRONIZE
        if not handle:
            return
        k32.WaitForSingleObject(handle, 0xFFFFFFFF)       # blocks until parent exits
    else:
        while os.getppid() == ppid:
            time.sleep(1)
    os._exit(0)


def _open_when_ready(url: str) -> None:
    for _ in range(60):
        try:
            urllib.request.urlopen(url + "health", timeout=1)
            webbrowser.open(url)
            return
        except OSError:
            time.sleep(0.5)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="studio_core")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="probe GPU, Blender, ComfyUI, model providers")
    sub.add_parser("projects", help="list projects")
    s = sub.add_parser("serve", help="start the REST API and dashboard on localhost")
    s.add_argument("--open", action="store_true", help="open the dashboard in the browser when ready")
    s = sub.add_parser("route", help="show which model a task would use"); s.add_argument("task")
    s = sub.add_parser("new", help="create a project"); s.add_argument("name")
    s = sub.add_parser("import", help="import reference files")
    s.add_argument("project"); s.add_argument("files", nargs="+"); s.add_argument("--note", default="")
    s = sub.add_parser("analyse", help="run vision analysis")
    s.add_argument("project"); s.add_argument("--ref"); s.add_argument("--force", action="store_true")
    s.add_argument("--model", help="vision model for this run, e.g. ollama/llava:latest")
    s = sub.add_parser("validate", help="run deterministic project checks"); s.add_argument("project")
    s = sub.add_parser("propose", help="draft a design brief proposal from requirements")
    s.add_argument("project"); g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", help="requirements as text"); g.add_argument("--file", help="requirements .txt/.md file")
    s.add_argument("--model", help="reasoning model for this run, e.g. ollama/qwen3:8b")
    sub.add_parser("models", help="list models per task: available, pinned, automatic pick")
    s = sub.add_parser("use", help="pin a model to a task (or 'auto' to unpin)")
    s.add_argument("task", choices=sorted(router.TASKS)); s.add_argument("model")
    s = sub.add_parser("proposals", help="list brief proposals"); s.add_argument("project")
    s = sub.add_parser("approve", help="adopt a proposal into the brief (user action)")
    s.add_argument("project"); s.add_argument("proposal"); s.add_argument("--concept", type=int, default=0)
    s = sub.add_parser("reject", help="reject a proposal")
    s.add_argument("project"); s.add_argument("proposal"); s.add_argument("--reason", default="")
    s = sub.add_parser("decide", help="approve or reject a recorded decision (user action)")
    s.add_argument("project"); s.add_argument("decision"); s.add_argument("status", choices=["approved", "rejected"])
    a = ap.parse_args(argv)

    try:
        if a.cmd == "serve":
            import uvicorn
            c = config.load()["api"]
            if c["host"] not in ("127.0.0.1", "localhost", "::1"):
                # the API has no authentication; refuse to expose it to a network
                print("Refusing to bind a non-localhost address: the API has no authentication.", file=sys.stderr)
                return 2
            url = f"http://{c['host']}:{c['port']}/"
            with socket.socket() as probe:
                if probe.connect_ex((c["host"], c["port"])) == 0:
                    print(f"Port {c['port']} is already in use (Studio may already be running: {url}).\n"
                          "Close the other Studio window first, or change api.port in config/studio.yaml.",
                          file=sys.stderr)
                    return 2
            threading.Thread(target=_exit_with_parent, daemon=True).start()
            if a.open:
                threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
            print(f"\n  Architectural Studio: {url}\n  Close this window or press Ctrl+C to stop. "
                  f"Port {c['port']} is released on exit.\n")
            uvicorn.run("studio_core.api:app", host=c["host"], port=c["port"], log_level="warning")
            return 0
        if a.cmd == "status":
            out = status.report()
        elif a.cmd == "projects":
            out = projects.list_projects()
        elif a.cmd == "route":
            c = router.pick(a.task)
            out = {"model": c["model"].id, "reason": c["reason"], "rejected": c["rejected"]}
        elif a.cmd == "new":
            out = projects.create_project(a.name)
        elif a.cmd == "import":
            out = [ingest.import_reference(a.project, f, a.note) for f in a.files]
        elif a.cmd == "analyse":
            out = (vision.analyse(a.project, a.ref, vision_model=a.model) if a.ref
                   else vision.analyse_all(a.project, a.force, vision_model=a.model))
        elif a.cmd == "propose":
            text = a.text if a.text else open(a.file, encoding="utf-8").read()
            rec = brief.propose(a.project, text, reasoning_model=a.model)
            print(brief.render(rec))
            return 0
        elif a.cmd == "models":
            out = router.overview()
        elif a.cmd == "use":
            out = router.set_choice(a.task, None if a.model == "auto" else a.model)
        elif a.cmd == "proposals":
            out = brief.list_proposals(a.project)
        elif a.cmd == "approve":
            out = brief.approve(a.project, a.proposal, a.concept)
        elif a.cmd == "reject":
            out = brief.reject(a.project, a.proposal, a.reason)
        elif a.cmd == "decide":
            out = projects.set_decision_status(a.project, a.decision, a.status)
        else:
            out = projects.validate(a.project)
            print(json.dumps(out, indent=2))
            return 0 if out["valid"] else 1
    except (projects.StudioError, providers.ProviderError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
