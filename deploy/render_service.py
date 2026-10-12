"""Render the system service using the installing account, without sed escaping."""
import argparse
from pathlib import Path
import re


def _escape(value: str) -> str:
    if any(char in value for char in "\n\r\0"):
        raise ValueError("Service values must be single-line strings")
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def render_service(template: Path, user: str, home: str, uid: int, repo_dir: str) -> str:
    if uid <= 0 or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]*\$?", user):
        raise ValueError("adhand must run as a non-root user")
    if not Path(home).is_absolute() or not Path(repo_dir).is_absolute():
        raise ValueError("Home and repository paths must be absolute")
    if any(char in repo_dir for char in "\n\r\0"):
        raise ValueError("Repository path must be a single-line string")
    replacements = {
        "@ADHAND_USER@": user, "@ADHAND_HOME@": _escape(home), "@ADHAND_UID@": str(uid),
        # WorkingDirectory takes a raw path, unlike Environment;
        # quoting it would make the leading quote part of the path.
        "@ADHAND_REPO@": repo_dir.replace("%", "%%"),
    }
    output = template.read_text()
    for token, value in replacements.items():
        output = output.replace(token, value)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("user")
    parser.add_argument("home")
    parser.add_argument("uid", type=int)
    parser.add_argument("repo_dir")
    args = parser.parse_args()
    print(render_service(Path(__file__).with_name("adhand.service"), args.user,
                         args.home, args.uid, args.repo_dir), end="")
