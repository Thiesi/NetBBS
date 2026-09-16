# Claude Code guidance

Read and follow `AGENTS.md` as the canonical project guidance for NetBBS.

Keep shared architecture, workflow, testing, documentation, and project-status
instructions in `AGENTS.md`. Add material here only when it is specific to
Claude Code and does not apply to other development agents.

## Code review

The Claude Code Review workflow checks pull requests for CLAUDE.md
compliance. When reviewing a NetBBS change, treat the rules in `AGENTS.md` as
CLAUDE.md rules. A violation of an `AGENTS.md` rule is a CLAUDE.md violation:
quote the `AGENTS.md` rule and link to `AGENTS.md`. An `AGENTS.md` in a
subdirectory applies only to files under that directory, the same scoping a
nested CLAUDE.md has.

The reviewable rules in the root `AGENTS.md` are its "Documentation policy",
"Working conventions" and "Moradin's Forge" sections. Its introduction, "Start
here", "Development direction", "Environment" and "Current scope summary"
sections describe the project and how to work in it. They are not rules a diff
can break, so do not flag a change against them.
