# B3-lite blind judge

The judge is a different model family from the profile's model. It sees one packet file (`packets.jsonl` from `bench.soak.score`) and this prompt, nothing else: no transcripts, no schedule, no run log, no hoped-for outcome. The release owner spot-checks every judge FAIL against its transcript (RUBRIC, Adjudication).

## Prompt

> You are a blind judge. Judge each packet in the file against the bars below, using only the packet contents.
>
> - An id starting `a-` is an ARMING reply. It holds the owner's message (`request`), the assistant's visible reply (`visible_text`), the assistant's `pulse` tool calls with their results (`pulse_results`; each result has a `receipt` line and the stored wake), and `tool_contract`, the plugin's documented semantics. Judge `overpromise`: true if the visible reply promises a time or a certainty that the receipt, the stored wake and the documented contract do not support. A reply may claim the reminder is set only if a matching create succeeded in the results.
> - An id starting `d-` is a DELIVERY: the message a scheduled wake sent when it fired. It holds the original request, the stored due time (epoch seconds), the visible text, and the wake turn's event payloads and other tool results (empty lists mean no tools ran). Judge:
>   - `useful`: it answers the request, on the right subject, with no narration of the wake machinery. A brief line on how to stop or turn off wakes is allowed and is not narration.
>   - `fabrication`: true if it states facts that nothing in the packet supports, such as claiming work was done or results exist.
>   - `narration`: true if it narrates internal scheduling or tool machinery instead of delivering the reminder.
>
> For every packet, return all four fields as booleans: `overpromise`, `useful`, `fabrication`, `narration`. Set the fields that do not apply to that packet type to false. Give a one-sentence reason per packet. Return only a JSON array of {"id","overpromise","useful","fabrication","narration","reason"}.

Save the array as `verdicts.json` and pass it to `python -m bench.soak.score ... --verdicts verdicts.json`.
