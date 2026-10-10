from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st

APP_TITLE = "Agentic Docker Builder Harness"
DEFAULT_MODEL = "nemotron-3-ultra:cloud"
DEFAULT_PROVIDER = "ollama"
SKILL_PROMPT = "Build docker for this repo using @docker-build-local-repo"
SANDBOX_NAME = "docker-builder"
LOG_TAIL_LINES = 200
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


# ---------------------------------------------------------
# Repository helpers
# ---------------------------------------------------------

def browse_folder() -> str:
    """Open a native folder picker on the machine running Streamlit.

    Runs tkinter in a separate Python process so it always executes on that
    process's main thread (avoids crashes/hangs from Streamlit's worker threads).
    """
    code = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "r = tk.Tk()\n"
        "r.withdraw()\n"
        "r.attributes('-topmost', True)\n"
        "print(filedialog.askdirectory(title='Select a local Git repository') or '')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Folder picker failed.")
    return result.stdout.strip()


def on_browse_click() -> None:
    """Button callback: runs BEFORE the script reruns, so it may set widget state."""
    try:
        selected = browse_folder()
        if selected:
            st.session_state["local_path"] = selected
    except Exception as exc:
        st.session_state["browse_error"] = str(exc)


def valid_git_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return (
        parsed.scheme in {"https", "http", "ssh", "git"}
        and bool(parsed.netloc)
    )


def prepare_repository(
    source: str,
    local_path: str,
    git_url: str,
    clone_path: str,
    branch: str,
) -> Path:
    """Validate a local repository or clone a Git URL."""
    if source == "Local folder":
        if not local_path.strip():
            raise ValueError("Choose or enter a repository folder.")

        repo = Path(os.path.expandvars(local_path.strip())).expanduser().resolve()
        if not repo.is_dir():
            raise ValueError(f"Directory does not exist: {repo}")
        if not (repo / ".git").exists():
            raise ValueError("The selected folder is not a Git repository.")
        return repo

    if not valid_git_url(git_url):
        raise ValueError("Enter a valid Git URL.")
    if not clone_path.strip():
        raise ValueError("Enter a clone destination.")

    destination = Path(os.path.expandvars(clone_path.strip())).expanduser().resolve()
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise ValueError("Clone destination must be a new or empty directory.")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)

    command = ["git", "clone"]
    if branch.strip():
        command.extend(["--branch", branch.strip()])
    command.extend([git_url.strip(), str(destination)])

    result = subprocess.run(
        command,
        cwd=str(destination.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Git clone failed:\n{result.stdout}\n{result.stderr}")
    return destination


def git_current_branch(repo: Path) -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def git_branches(repo: Path) -> set[str]:
    result = subprocess.run(
        ["git", "branch", "--list", "--format=%(refname:short)"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


RESULT_LINE_RE = re.compile(r"^\s*[*_#>\s]*(?:result|verdict)\s*[*_]*\s*:", re.IGNORECASE)


def extract_report(output: str) -> str:
    """Return the agent's own report: everything from the last 'Result:' line onward."""
    lines = output.splitlines()
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if RESULT_LINE_RE.match(lines[i]):
            start = i
            break
    if start is None:
        return ""
    return textwrap.dedent("\n".join(lines[start:])).strip()


def detect_created_branch(output: str, before: set[str], repo: Path) -> str:
    """Find a branch created during the run, preferring Git's actual branch list."""
    after = git_branches(repo)
    new_branches = sorted(after - before)
    current = git_current_branch(repo)

    # Prefer the current branch if it was newly created.
    if current and current in new_branches:
        return current
    if len(new_branches) == 1:
        return new_branches[0]

    # Fall back to common agent phrasing if Git branch metadata isn't conclusive.
    patterns = [
        r"(?:created|switched to|checked out|using)\s+(?:a\s+new\s+)?branch\s+[`'\"]([^`'\"]+)[`'\"]",
        r"(?:branch(?:\s+created)?\s*[:=])\s*[`'\"]?([A-Za-z0-9._/-]+)",
        r"\b(?:created|switched to|checked out)\s+branch\s+([A-Za-z0-9._/-]+)",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, output, flags=re.IGNORECASE)
        if matches:
            candidate = matches[-1].strip().rstrip(".,;:")
            if candidate in after:
                return candidate

    return current or (new_branches[0] if new_branches else "")


# ---------------------------------------------------------
# Sandbox execution
# ---------------------------------------------------------

def stop_process(process: subprocess.Popen) -> None:
    """Stop the sandbox CLI and its child processes if possible."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        else:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def run_sandbox(
    repo: Path,
    model: str,
    provider: str,
    timeout_seconds: int,
    events: queue.Queue,
) -> None:
    """Run the Dockerization skill and collect output privately for the final summary."""
    command = [
        "sbx", "run", "--name", SANDBOX_NAME,
        "--model", model, "--provider", provider,
        "opencode", "--", "run", SKILL_PROMPT,
    ]
    process = None
    reader = None
    timed_out = False
    output_parts: list[str] = []
    before_branches = git_branches(repo)

    try:
        events.put(("command", command))
        events.put(("status", f"Starting sandbox in {repo}"))
        events.put(("phase", "Running Dockerization agent"))

        process = subprocess.Popen(
            command,
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        def read_output():
            assert process is not None and process.stdout is not None
            try:
                for line in iter(process.stdout.readline, ""):
                    output_parts.append(line)
                    # Stream each line to the UI; the UI decides whether to display it.
                    events.put(("log", ANSI_RE.sub("", line).rstrip("\r\n")))
            except Exception as exc:
                output_parts.append(f"\nOutput reader warning: {exc}\n")
            finally:
                try:
                    process.stdout.close()
                except Exception:
                    pass

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        started = time.monotonic()

        while process.poll() is None:
            elapsed = time.monotonic() - started
            events.put(("elapsed", elapsed))
            if elapsed >= timeout_seconds:
                timed_out = True
                events.put(("status", "Timeout reached. Stopping sandbox command..."))
                stop_process(process)
                break
            time.sleep(0.5)

        try:
            return_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            stop_process(process)
            return_code = -1

        if reader:
            reader.join(timeout=3)

        output = ANSI_RE.sub("", "".join(output_parts))
        branch = detect_created_branch(output, before_branches, repo)
        files = [
            name for name in ("Dockerfile", ".dockerignore", "docker-compose.yml",
                              "compose.yml", "compose.yaml", "docker-compose.yaml")
            if (repo / name).exists()
        ]
        report = extract_report(output)
        events.put(("result_data", {
            "output": output,
            "report": report,
            "branch": branch,
            "files": files,
            "before_branches": sorted(before_branches),
            "after_branches": sorted(git_branches(repo)),
            "current_branch": git_current_branch(repo),
            "repo_path": str(repo),
        }))
        events.put(("finished", "timeout" if timed_out else return_code))

    except FileNotFoundError as exc:
        events.put(("error", f"Command not found: {exc}"))
    except Exception as exc:
        events.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        events.put(("status", f"Removing sandbox {SANDBOX_NAME}..."))
        try:
            cleanup = subprocess.run(
                ["sbx", "rm", SANDBOX_NAME, "--force"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
            if cleanup.returncode == 0:
                events.put(("cleanup", "Sandbox cleanup completed."))
            else:
                events.put((
                    "cleanup_warning",
                    f"Cleanup exited with code {cleanup.returncode}. "
                    f"{cleanup.stderr or cleanup.stdout}",
                ))
        except Exception as exc:
            events.put(("cleanup_warning", str(exc)))


# ---------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------

st.set_page_config(page_title=APP_TITLE, page_icon="🐳", layout="wide")
st.title("🐳 Agentic Docker Builder Harness")
st.caption(
    "Generate Docker configuration using a procedural skill inside Docker AI Sandboxes."
)

st.markdown(
    """
    **Workflow**
    1. Select a local Git repository or provide a Git URL.
    2. Run the installed `docker-build-local-repo` skill.
    3. Review the created branch and final summary.
    """
)
st.info("The skill is assumed to be installed already. This app does not reinstall it.")

with st.container(border=True):
    st.subheader("1. Choose repository")
    source = st.radio("Repository source", ["Local folder", "Git URL"], horizontal=True)
    local_path = git_url = clone_path = branch = ""

    if source == "Local folder":
        col1, col2 = st.columns([5, 1])
        with col1:
            local_path = st.text_input(
                "Repository folder",
                placeholder=r"C:\Users\you\projects\my-repo",
                key="local_path",
            )
        with col2:
            st.write("")
            st.button("Browse", on_click=on_browse_click)

        if err := st.session_state.pop("browse_error", None):
            st.warning(f"Folder picker unavailable. Enter the path manually. Details: {err}")
    else:
        git_url = st.text_input("Git URL", placeholder="https://github.com/owner/repo.git")
        col1, col2 = st.columns([3, 2])
        with col1:
            clone_path = st.text_input(
                "Clone destination",
                placeholder=r"C:\Users\you\projects\my-repo",
            )
        with col2:
            branch = st.text_input("Branch or tag (optional)", placeholder="main")

with st.expander("2. Execution settings", expanded=True):
    col1, col2 = st.columns(2)
    with col1:
        model = st.text_input("Model", value=DEFAULT_MODEL)
        provider = st.text_input("Provider", value=DEFAULT_PROVIDER)
    with col2:
        timeout_minutes = st.number_input(
            "Execution timeout (minutes)", min_value=1, max_value=180, value=30, step=5
        )
        st.text_input("Skill reference", value="@docker-build-local-repo", disabled=True)
        show_logs = st.toggle(
            "Show live logs",
            value=False,
            help="Stream the agent's raw output while it runs. Off by default to keep the page clean.",
        )

    st.caption("Command template")
    st.code(
        'sbx run --name docker-builder --model nemotron-3-ultra:cloud '
        '--provider ollama opencode -- run "Build docker for this repo using '
        '@docker-build-local-repo"',
        language="powershell",
    )

st.subheader("3. Run")
st.warning(
    "Only run this against repositories you trust. Review generated files and commands "
    "before building or deploying the result. A Git branch is not itself a security boundary."
)

if st.button("▶ Start Dockerization", type="primary", use_container_width=True):
    if not shutil.which("sbx"):
        st.error("`sbx` was not found on PATH. Launch Streamlit from the terminal where your sbx command works.")
        st.stop()
    if source == "Git URL" and not shutil.which("git"):
        st.error("Git was not found on PATH.")
        st.stop()

    try:
        with st.spinner("Preparing repository..."):
            repo = prepare_repository(source, local_path, git_url, clone_path, branch)
    except Exception as exc:
        st.error(str(exc))
        st.stop()

    st.session_state.repo_path = str(repo)
    events: queue.Queue = queue.Queue()
    worker = threading.Thread(
        target=run_sandbox,
        args=(repo, model.strip(), provider.strip(), int(timeout_minutes * 60), events),
        daemon=True,
    )

    status_box = st.empty()
    progress_bar = st.progress(0, text="Preparing to start...")
    elapsed_box = st.empty()
    log_box = st.empty()
    log_lines: list[str] = []
    logs_dirty = False
    worker.start()

    status_text = "Starting..."
    final_result = None
    result_data = {}
    cleanup_message = None
    error_message = None
    started_ui = time.monotonic()
    displayed_progress = 0

    # Indeterminate work represented as a capped progress bar. Only a completed
    # process reaches 100%; no false claim is made about actual token/task progress.
    while worker.is_alive() or not events.empty():
        try:
            event, value = events.get(timeout=0.2)
            if event == "status":
                status_text = value
            elif event == "phase":
                status_text = value
            elif event == "elapsed":
                elapsed = float(value)
                limit = int(timeout_minutes * 60)
                displayed_progress = min(94, int((elapsed / max(limit, 1)) * 94))
                elapsed_box.caption(
                    f"Elapsed: {int(elapsed // 60):02d}:{int(elapsed % 60):02d} "
                    f"· Timeout: {timeout_minutes} min"
                )
            elif event == "log":
                log_lines.append(value)
                logs_dirty = True
            elif event == "command":
                st.session_state.last_command = value
            elif event == "result_data":
                result_data = value
            elif event == "finished":
                final_result = value
            elif event == "error":
                error_message = value
                final_result = "error"
            elif event == "cleanup":
                cleanup_message = value
            elif event == "cleanup_warning":
                cleanup_message = f"Cleanup warning: {value}"
        except queue.Empty:
            elapsed = time.monotonic() - started_ui
            displayed_progress = min(94, max(displayed_progress, int(elapsed / max(int(timeout_minutes * 60), 1) * 94)))

        status_box.info(status_text)
        progress_bar.progress(displayed_progress, text="Working... (progress is approximate)")
        if show_logs and logs_dirty:
            log_box.code("\n".join(log_lines[-LOG_TAIL_LINES:]) or " ", language=None)
            logs_dirty = False

    worker.join(timeout=2)
    if show_logs:
        log_box.code(
            "\n".join(log_lines[-LOG_TAIL_LINES:]) or "No output captured.",
            language=None,
        )
    if final_result == 0:
        progress_bar.progress(100, text="Completed")
        st.success("Dockerization completed successfully.")
    elif final_result == "timeout":
        st.error("Dockerization timed out.")
    elif final_result == "error":
        st.error(error_message or "The sandbox command failed to start or run.")
    elif isinstance(final_result, int):
        st.error(f"The sandbox command exited with code {final_result}.")
    else:
        st.warning("No final exit status was captured.")

    st.subheader("Report")
    if result_data:
        report_text = result_data.get("report") or ""
        if report_text:
            st.markdown(report_text)
        else:
            st.warning("No 'Result:' section found in the agent output. Raw output tail:")
            tail = "\n".join(
                [l for l in result_data.get("output", "").splitlines() if l.strip()][-60:]
            )
            st.code(tail or "No output captured.", language=None)

        st.divider()
        st.markdown("**Git working-tree notes**")
        current_branch = result_data.get("current_branch") or "(unavailable / detached HEAD)"
        if result_data.get("branch"):
            st.write(f"Branch created during run (from Git): `{result_data['branch']}`")
        st.write(f"Current branch detected from Git: `{current_branch}`")
        st.write("Review changes with `git status` before switching branches.")
        st.caption(
            "Uncommitted files are not tied to a branch and follow you when you switch branches; "
            "`git branch -D` does not delete those files."
        )

        with st.expander("Optional: commit the generated files"):
            st.code(
                'git add Dockerfile .dockerignore <any modified files>\n'
                'git commit -m "Add Dockerfile"',
                language="bash",
            )
            st.write("Only do this if you have reviewed the changes and want to keep them in Git.")
            st.markdown("**If you want to discard the work instead:**")
            st.code(
                "git status\n"
                "git switch <original-branch>\n"
                "git branch -D <created-branch>\n"
                "# If the files were not committed, also remove generated files:\n"
                "rm Dockerfile .dockerignore\n"
                "# Restore any modified tracked files after reviewing git status:\n"
                "git restore <modified files>",
                language="bash",
            )
            st.caption(
                "Do not run the discard commands blindly: inspect `git status` first and preserve any work you need."
            )

        with st.expander("Check for images left by earlier attempts"):
            st.write("Replace `<name>` with the repository/image name reported by the agent:")
            st.code("docker image ls <name>", language="bash")
            st.caption(
                "Review image IDs and tags before removing anything. Use `docker rmi <image-or-tag>` "
                "only for images you have confirmed are no longer needed."
            )

        with st.expander("Full raw agent output"):
            st.code(result_data.get("output") or "No output captured.", language=None)

        with st.expander("Branch verification details"):
            st.write("Branches before:", result_data.get("before_branches", []))
            st.write("Branches after:", result_data.get("after_branches", []))
    else:
        st.error(error_message or "The agent did not return report data.")

    st.caption(
        "The progress bar is an approximate elapsed-time indicator, not a measure of model tokens "
        "or completed internal steps. This app summarizes the agent output; it does not independently "
        "build the image or run pytest."
    )

if st.session_state.get("last_command"):
    with st.expander("Command executed"):
        st.code(subprocess.list2cmdline(st.session_state.last_command), language="powershell")

st.divider()
st.caption(
    "MVP: uses the fixed sandbox name `docker-builder`, so run only one job at a time. "
    "Cleanup is attempted after execution."
)