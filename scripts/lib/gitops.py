"""Small git helpers for publishing generated files."""

from __future__ import annotations

from pathlib import Path

from . import util


def push_url(url: str) -> str:
    """Embed a token so ``git push`` never has to ask for credentials.

    Which token depends on *which* repository. The workflow's own token can only
    write the repository the workflow runs in, and that is exactly the right
    credential for that one -- the ``index`` branch lives there. Anything else,
    the consumer-facing repository in particular, needs the push token (a PAT in
    CI). Each falls back to the other, so a missing token still means a push
    rather than a prompt: a runner has no terminal to prompt with.
    """
    import os

    if not url.startswith("https://github.com/"):
        return url
    slug = url[len("https://github.com/"):].removesuffix(".git").strip("/").lower()
    own = os.environ.get("GITHUB_REPOSITORY", "").strip().lower()
    workflow = ("GH_TOKEN", "GITHUB_TOKEN")
    push = ("BUILDCACHE_PUSH_TOKEN",)
    order = workflow + push if own and slug == own else push + workflow
    for name in order:
        token = os.environ.get(name, "").strip()
        if not token:
            continue
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
    # The generated files are written with LF on purpose; without this the run
    # drowns in one "LF will be replaced by CRLF" warning per file.
    env = util.subprocess_env(util.load_config())
    name, email = util.git_identity(util.load_config())
    add_args = ["git", "-C", str(repo), "add", "-A"]
    if paths:
        add_args = ["git", "-C", str(repo), "add", "--"] + list(paths)
    util.run(add_args, env=env, quiet=True)
    staged = util.run(
        ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
        env=env,
        check=False,
        quiet=True,
    )
    committed = staged.returncode != 0
    if not committed:
        util.log(f"{repo.name}: nothing to commit")
    else:
        util.run(
            [
                "git", "-C", str(repo),
                "-c", f"user.name={name}",
                "-c", f"user.email={email}",
                "commit", "-m", message,
            ],
            env=env,
            quiet=True,
        )
    if push:
        branch = current_branch(repo)
        # Someone else (CI, or another checkout) may have published while this one
        # was writing, and a failed fetch would leave a stale remote-tracking ref --
        # then the push is rejected, half the round is published and the rest is
        # not. Sync and try again instead, and push even when this round had
        # nothing new to commit: an earlier attempt may have committed locally and
        # died before the push.
        for attempt in (1, 2):
            sync_branch(repo, remote)
            result = util.run(
                ["git", "-C", str(repo), "push", remote, branch],
                env=env,
                quiet=True,
                check=False,
            )
            if result.returncode == 0:
                return committed
            util.warn(f"{repo.name}: push to {remote}/{branch} failed, retrying (attempt {attempt})")
        util.die(f"{repo.name}: could not push {remote}/{branch} after two attempts")
    return committed


def ensure_checkout(
    config: dict,
    url: str,
    directory: Path,
    branch: str | None = None,
    reset: bool = False,
) -> Path:
    """Clone (or refresh) a working checkout used for generated output.

    The clone keeps the push credential in its remote: everything this checkout
    exists for ends in a commit that has to be pushed back. A remote without a
    token makes git fall back to whatever the environment offers -- in CI that is
    the workflow's own token, which cannot write a *second* repository, so the
    push comes back as "denied to github-actions[bot]".
    """
    directory = Path(directory)
    env = util.subprocess_env(config)
    if not (directory / ".git").is_dir():
        if directory.exists():
            # A generated-only directory from an earlier run: safe to recreate.
            util.rmtree(directory)
        util.ensure_dir(directory.parent)
        args = ["git", "clone", push_url(url), str(directory)]
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


def sync_branch(repo: Path, remote: str = "origin") -> None:
    """Put the local branch on top of the remote, keeping local commits.

    The generated repositories are written from more than one place (CI and a
    developer's machine), so a checkout is routinely behind -- and a plain push
    then fails with "non-fast-forward", which is how a local finalize ends up
    doing half its work. Rebasing instead of resetting keeps whatever this
    checkout produced (a repaired recipe, say) and still fast-forwards.
    """
    repo = Path(repo)
    if not (repo / ".git").is_dir():
        return
    env = util.subprocess_env(util.load_config())
    branch = current_branch(repo)
    # Fetch the branch explicitly and rebase onto FETCH_HEAD: a checkout made for
    # a single generated branch does not necessarily have an origin/<branch>
    # remote-tracking ref at all.
    util.run(
        ["git", "-C", str(repo), "fetch", remote, branch], env=env, quiet=True, check=False
    )
    result = util.run(
        ["git", "-C", str(repo), "rebase", "FETCH_HEAD"],
        env=env,
        capture=True,
        quiet=True,
        check=False,
    )
    if result.returncode != 0:
        # A conflict here means the same generated files were written twice, from
        # two checkouts. Leave the repo usable and let the caller decide.
        util.run(
            ["git", "-C", str(repo), "rebase", "--abort"], env=env, quiet=True, check=False
        )
        util.warn(
            f"{repo.name}: cannot rebase onto {remote}/{branch} "
            f"(another publish wrote the same files?); push will be attempted as is"
        )


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
    # Fetch the branch explicitly: a checkout made for a single generated branch
    # does not necessarily carry an origin/<branch> remote-tracking ref, and a
    # stale (or silently failed) fetch is how a publish ends up half done.
    util.run(
        ["git", "-C", str(directory), "fetch", "origin", branch],
        env=env,
        quiet=True,
        check=False,
    )
    util.run(
        ["git", "-C", str(directory), "checkout", "--quiet", branch],
        env=env,
        capture=True,
        quiet=True,
        check=False,
    )
    util.run(
        ["git", "-C", str(directory), "reset", "--hard", "FETCH_HEAD"],
        env=env,
        capture=True,
        quiet=True,
        check=False,
    )
    return directory
