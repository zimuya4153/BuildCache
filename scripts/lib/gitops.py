"""Small git helpers for publishing generated files."""

from __future__ import annotations

from pathlib import Path

from . import util


def push_url(url: str) -> str:
    """Embed ``BUILDCACHE_PUSH_TOKEN`` so CI can push to a second repository."""
    import os

    token = os.environ.get("BUILDCACHE_PUSH_TOKEN", "").strip()
    if token and url.startswith("https://github.com/"):
        return url.replace("https://github.com/", f"https://x-access-token:{token}@github.com/")
    return url


def current_branch(repo: Path) -> str:
    return util.run_out(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], check=False)


def commit_and_push(
    repo: Path,
    message: str,
    paths: list[str] | None = None,
    push: bool = True,
    remote: str = "origin",
) -> bool:
    """Stage, commit and push; returns False when there was nothing to commit."""
    repo = Path(repo)
    name, email = util.git_identity(util.load_config())
    add_args = ["git", "-C", str(repo), "add", "-A"]
    if paths:
        add_args = ["git", "-C", str(repo), "add", "--"] + list(paths)
    util.run(add_args, quiet=True)
    staged = util.run(
        ["git", "-C", str(repo), "diff", "--cached", "--quiet"], check=False, quiet=True
    )
    if staged.returncode == 0:
        util.log(f"{repo.name}: nothing to commit")
        return False
    util.run(["git", "-C", str(repo), "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", message], quiet=True)
    if push:
        branch = current_branch(repo)
        util.run(["git", "-C", str(repo), "push", remote, branch], quiet=True)
    return True


def ensure_checkout(
    config: dict,
    url: str,
    directory: Path,
    branch: str | None = None,
    reset: bool = False,
) -> Path:
    """Clone (or refresh) a working checkout used for generated output."""
    directory = Path(directory)
    env = util.subprocess_env(config)
    if not (directory / ".git").is_dir():
        if directory.exists():
            # A generated-only directory from an earlier run: safe to recreate.
            util.rmtree(directory)
        util.ensure_dir(directory.parent)
        args = ["git", "clone", url, str(directory)]
        if branch:
            args += ["--branch", branch]
        util.run(args, env=env)
        return directory
    util.run(["git", "-C", str(directory), "fetch", "origin"], env=env, quiet=True, check=False)
    if branch:
        util.run(
            ["git", "-C", str(directory), "checkout", "--quiet", branch],
            capture=True,
            check=False,
            quiet=True,
        )
        if reset:
            util.run(
                ["git", "-C", str(directory), "reset", "--hard", f"origin/{branch}"],
                quiet=True,
                check=False,
            )
    return directory


def branch_exists(config: dict, owner: str, repo: str, branch: str) -> bool:
    from . import gh

    data = gh.api_or_none(f"repos/{owner}/{repo}/branches/{branch}", config=config)
    return isinstance(data, dict) and bool(data.get("name"))


def ensure_orphan_branch(config: dict, owner: str, repo: str, branch: str, proxy: str | None = None) -> bool:
    """Create an empty branch used purely as the target of release tags.

    Release tags are created on this branch so that ``main`` never gains a tag
    per published SDK version.
    """
    if branch_exists(config, owner, repo, branch):
        return False
    url = push_url(f"https://github.com/{owner}/{repo}.git")
    env = util.subprocess_env(config, proxy=proxy)
    bot_name, bot_email = util.git_identity(config)
    work = util.absolute(".work") / f"{repo}-{branch}-branch"
    util.rmtree(work)
    util.run(["git", "clone", "--depth", "1", url, str(work)], env=env, quiet=True)
    util.run(["git", "-C", str(work), "checkout", "--orphan", branch], env=env, quiet=True)
    util.run(["git", "-C", str(work), "rm", "-rf", "--quiet", "."], env=env, check=False)
    util.write_text(
        Path(work) / "README.md",
        f"# `{branch}`\n\n"
        "This branch exists for one reason: it is the *target* of every release\n"
        "tag. Release tags are created here so that the `main` branch never\n"
        "accumulates one tag per published SDK version.\n\n"
        "The prebuilt SDK artifacts themselves are attached to GitHub Releases;\n"
        "nothing is committed to this branch beyond this file.\n",
    )
    util.run(["git", "-C", str(work), "add", "-A"], env=env, quiet=True)
    util.run(
        [
            "git", "-C", str(work),
            "-c", f"user.name={bot_name}",
            "-c", f"user.email={bot_email}",
            "commit", "--quiet", "-m", f"chore: create '{branch}' branch for release tags",
        ],
        env=env,
        quiet=True,
    )
    util.run(["git", "-C", str(work), "push", "origin", branch], env=env, quiet=True)
    util.log(f"created '{branch}' branch in {owner}/{repo} for release tags")
    return True


def ensure_branch(
    config: dict,
    owner: str,
    repo: str,
    branch: str,
    directory: Path,
    seed: tuple[str, str] | None = None,
    proxy: str | None = None,
) -> Path:
    """Check out ``branch`` into ``directory``, creating the branch when needed."""
    url = push_url(f"https://github.com/{owner}/{repo}.git")
    env = util.subprocess_env(config, proxy=proxy)
    directory = Path(directory)
    if not branch_exists(config, owner, repo, branch):
        if seed is None:
            return directory
        bot_name, bot_email = util.git_identity(config)
        util.rmtree(directory)
        util.run(["git", "clone", "--depth", "1", url, str(directory)], env=env, quiet=True)
        util.run(["git", "-C", str(directory), "checkout", "--orphan", branch], env=env, quiet=True)
        util.run(["git", "-C", str(directory), "rm", "-rf", "--quiet", "."], env=env, check=False)
        util.write_text(directory / seed[0], seed[1])
        util.run(["git", "-C", str(directory), "add", "-A"], env=env, quiet=True)
        util.run(
            [
                "git", "-C", str(directory),
                "-c", f"user.name={bot_name}",
                "-c", f"user.email={bot_email}",
                "commit", "--quiet", "-m", f"chore: create '{branch}' branch",
            ],
            env=env,
            quiet=True,
        )
        util.run(["git", "-C", str(directory), "push", "-u", "origin", branch], env=env, quiet=True)
        util.log(f"created '{branch}' branch in {owner}/{repo}")
        return directory
    if not (directory / ".git").is_dir():
        util.rmtree(directory)
        util.ensure_dir(directory.parent)
        util.run(["git", "clone", "--branch", branch, url, str(directory)], env=env, quiet=True)
        return directory
    util.run(["git", "-C", str(directory), "fetch", "origin"], env=env, quiet=True, check=False)
    util.run(
        ["git", "-C", str(directory), "checkout", "--quiet", branch],
        env=env,
        capture=True,
        quiet=True,
        check=False,
    )
    util.run(
        ["git", "-C", str(directory), "reset", "--hard", f"origin/{branch}"],
        env=env,
        capture=True,
        quiet=True,
        check=False,
    )
    return directory
