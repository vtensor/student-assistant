You read a student-assistant conversation turn and extract durable facts about
the student into a small set of memory entries. Return JSON only.

A memory entry is useful only if it would help a future session personalize
better. Examples:
- "prefers video over text" (preference)
- "struggles with sign conventions in ray diagrams" (struggle)
- "wants to score above 80 in Math by year-end" (goal)
- "studies 6-7 pm on weekdays" (schedule)
- "finished revising quadratic equations" (milestone)

Rules:
- Output ONLY items the student stated about themselves or revealed clearly.
- Do NOT echo profile data already known (board, grade, weak topics list).
- Do NOT extract transient state ("I'm tired tonight").
- One concise sentence per entry, max 200 chars.
- Set confidence between 0 and 1. Below 0.7 means do not include.
- Set valid_until ONLY for time-bounded facts (e.g. exam date, holiday).
- Return an empty list if nothing durable is present.

Output schema:
{{
  "memories": [
    {{
      "category": "preference|struggle|goal|schedule|milestone",
      "content": "...",
      "confidence": 0.0-1.0,
      "valid_until": "YYYY-MM-DD" | null
    }}
  ]
}}

Conversation turn:
USER: {user_message}
ASSISTANT: {assistant_message}
