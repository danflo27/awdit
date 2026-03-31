# Repo-Wide Black Hat Auditing Notes

## Standard Mode

Standard mode is what we are building: a medium amount of robust agents looking across the entire codebase.

## New Idea: Swarm Mode

Spawn a ton of mini folders. Let them use the shared resource context, but prompt each of them with:

> "You are playing a capture the flag game. Find a vulnerability. Hint: look at (file). Write the most serious one to runs/(run name)/out/report.txt"

An agent will get that prompt for every single file.

We will try to spawn as few agents as possible somehow.

Further design TBD.
