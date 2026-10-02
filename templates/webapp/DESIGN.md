# Design: the notes app

The feel: a quiet tool for one person. Dark surface, one accent, generous space, nothing that moves by itself. The list is the page; the field and the button sit under it. Text is left aligned, controls line up in one column, and every control says what it does in words.

References by name: the calm of a paper notebook, the plainness of a terminal, the focus of a single-column reader.

## Don't list

- No gradients. A surface is one flat colour from `src/tokens.css`.
- No emoji as icons. A control is a word, or a shape drawn from the tokens.
- No hero section. The first thing on the page is the list.
- No placeholder data. The panel starts empty or with what the server sent, never with "Lorem ipsum".
- No raw colour, font size or radius. Every value is a token name; the token gate fails one that is not.
- No animation that carries meaning. A state change is a redraw, not a performance.

## Copy rules

- Say what a control does, in the imperative: "Add note", "Remove". The tooltip explains the effect for the row it is on.
- A refusal says what happened and what to do next, in one sentence, and never blames the person.
- No exclamation marks, no "Oops", no "successfully".
- Name the thing, not the type: "note", not "item" or "entry".
