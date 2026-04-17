> **Archived 2026-04-17.** Still-open UX items rolled into [roadmap/ux.md](../roadmap/ux.md). Preserved for historical context; do not treat as current.

# Notes from prototype testing

03-29-2026
- In the future, plan on making an awesome ASCII art style TUI display on startup and throughout. Medium priority for now
- effective config summary needs cleaner alignment/spacing/color. Make a note somewhere permanant that we should be follwing CRAP desiign principles (color, repetition, alignment, proximity)
- Resource default notes should cleanly seperate the list of resources, or say none, and then say "note for user:..." 
    - Shared resources should follow the same exact design
- I dont want to manually create the work label and work key. Maybe automatically create them, and then have the user approve, or edit 
- The `Dispatch mode override [Enter=foreground/foreground/background]:  ` command is unclear. Lets dicsuss its function and how it should look


03-30-2026
- The in line instructions should not even be an option
- These three inputs (
Work label: test3
Work key: testkey3
Instructions source [inline/file]: inline
Instructions: Review this repo for cohesiveness, correctness based off of the docs, and look for security ulnerabilities
)  should not be in the CLI. The label and key should autogenerate and the user confirms or edits, and the only prompts the agent should have are the config/prompts/[slot].md file for that specific agent. The orchestrator will need a slot prompt as well. We will generate the prompts together later. 

New:
- prototype logging did not get generated or created anywhere
- We need an orchestrator.md file in the config prompts, and lets also try to spawn the rchestrator, who will dispatch hunter_1 with the correct prompt, and alert the user when  hunter_1 is finished
Use / edit / exit? [Y/e/n] prompt for shared resources is not clear -- add a space above and below the note, and change Use to proceed 
-