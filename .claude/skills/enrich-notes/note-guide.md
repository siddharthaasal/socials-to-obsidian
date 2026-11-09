# Writing a note for one saved Instagram post

Someone saved these Instagram posts because something in each was worth keeping: a
recipe, a book, a place, an idea, a workout, a tip. Your note replaces the post, so
they never have to rewatch it.

## Input

`data/packets/<id>.md` has the caption, a Whisper transcript of any video, and the
absolute paths of the post's images and of frames taken from its videos at even
intervals. Open the packet, then view **every** image it lists with the Read tool.
On-screen text in frames and slides often carries the real content.

The transcript is raw Whisper output. Hindi comes out in Devanagari with English words
mixed in, and names are often misheard (e.g. "नीच्चा" is Nietzsche). Use the caption and
on-screen text to get names right. If the video is only music, the transcript may be
song lyrics or garbage: ignore it then.

## Output

Write the JSON to the "Result file" path in the packet (`data/claude/<id>.json`).
It needs exactly these fields:

```json
{
  "useful": true,
  "reason": "",
  "title": "Beyond Good and Evil - where your morality comes from",
  "type": "philosophy",
  "tags": ["morality", "nietzsche"],
  "links": ["Friedrich Nietzsche", "Beyond Good and Evil"],
  "content": "Markdown body of the note"
}
```

### `content`

- Extract, don't summarize. Keep every specific: ingredients and quantities, steps,
  names, numbers, prices, places, book titles, the actual argument and its reasoning.
  Cut only filler: greetings, hooks, repetition, "follow for more", "comment below",
  "link in bio", hashtags, sponsorship lines, and anything about the creator.
- Length follows substance. If nearly the whole post is useful, write nearly all of it.
  A one-idea post gets a few lines.
- Write in English. Many posts are in Hindi or Hinglish: translate them naturally.
- Write it as the note itself, not as a description of a post. No "In this reel...",
  "The creator says...", or "This post explains...".
- Use Markdown structure where it helps: `##` sections, lists, numbered steps, tables.
  Don't repeat the title as a heading.

### `useful` and `reason`

Set `useful` to false when there is nothing worth keeping: pure memes and jokes,
aesthetic or vibe posts, music with no information, ads with nothing concrete, posts
too blurry or empty to read. Then give a one-line `reason`, and leave the other fields
empty (empty strings and lists, type `"other"`). A single sharp idea, quote, or
recommendation counts as useful. When `useful` is true, `reason` is `""`.

### Other fields

- `title`: specific and descriptive, at most 80 characters, no emojis, no trailing
  period. It names what the note gives you, e.g. "Dal makhani without cream".
- `type`: the closest of: recipe, food-and-places, travel, books, film-and-tv, music,
  philosophy, psychology, self-improvement, productivity, career, money, tech, science,
  history, health, fitness, relationships, design-and-art, language-and-writing,
  shopping, home-and-diy, other.
- `tags`: 2 to 6 lowercase, hyphenated topic tags that add to `type` and don't repeat it.
- `links`: up to 8 proper names worth their own page in a vault: books, authors,
  people, places, restaurants, products, named concepts. Use their canonical full
  names ("Friedrich Nietzsche", not "Nietzsche"). Never the post's creator.
