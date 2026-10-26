# Handpicked: Product

## Problem

Plex gives every user on a server the same recommendation shelves, and the self-hosted tools that fill the gap mostly do one of two things: auto-request titles based on watch history (SuggestArr), or ask an LLM for ideas (Recommendarr). Neither serves the person who actually runs a household Plex server and wants to curate what the family watches.

In a typical household setup:

- One admin manages the server and the *arr stack and controls what gets added.
- Family members have their own Plex users but do not use Seerr, and the admin may not want them to.
- The library is modest and mostly hand-picked, so discovering things *within* it is low value. Finding good things *not yet in it*, for specific people, is high value.

## Positioning

Recommendations for the person who runs the household Plex server, not just the people watching it.

The admin is the curator. The system suggests titles for each household member, the admin approves with full context, the title gets delivered to that person's home screen, and the system learns whether it landed.

## The full loop (long-term vision)

1. **Suggest:** per-user candidates from each person's own watch history.
2. **Approve:** the admin reviews with context (who it is for, and why) and approves or declines.
3. **Deliver:** once available, the title appears on the intended person's Plex home screen, not buried in the library.
4. **Measure:** track whether the person watched, abandoned, or ignored it, and feed that back into future suggestions.

v1 covers Suggest and Approve. Deliver and Measure come next, and are the main differentiator.

## How this differs from existing tools

| Tool | What it does | Gap this project fills |
|---|---|---|
| SuggestArr | Per-user TMDB-based suggestions, auto-requested to Seerr; optional LLM mode; cleanup of unwatched requests | Approval happens with no context; nothing delivers the title to the intended person; no outcome tracking that improves suggestions |
| Recommendarr | Sends library and watch history to an LLM, shows suggestions in a web UI | Not grounded in retrieval; weak per-user support; no request, delivery, or feedback loop |
| Shortlist | Per-user "Picked for You" rows inside Plex using label restrictions | Only recommends what you already own; no acquisition |
| Kometa | Collection and metadata management | Not a recommender |

Note: Seerr's manual approval queue is a fine gate, and this project uses it rather than replacing it. The approval step alone is not the differentiator. The context around it, the delivery after it, and the measurement of outcomes are.

## Parked ideas

- **Group recommendations ("movie night"):** pick something several household members would all like, within constraints like rating and runtime. Interesting and underserved, but not committed. Revisit after the core loop works.
- **Streaming availability overlap:** use TMDB watch-provider data to flag titles already available on services the household subscribes to. Possibly useful; future exploration only.
- **Kid-aware curation:** richer parental context beyond certification filters (why something is rated as it is, age-appropriate progression). A refinement for later.

## Rejected ideas

- **Storage-aware recommendations:** the disk space a title will use is hard to predict (quality profiles, release availability), so it is not worth the effort.

## Public release considerations

- **TMDB API terms:** attribution is required, and commercial use has its own rules. Review before any paid offering.
- **Plex Pass dependency:** per-user delivery via label restrictions needs Plex Pass on the admin account and a recent Plex Media Server version. This narrows the audience somewhat.
- **LLM features:** optional and bring-your-own-key only. The core pipeline must work with no AI provider configured.
- **Seerr, not Overseerr:** target the Seerr API. Overseerr is superseded.
- **Audience:** enthusiast-sized. Think a solid open-source project for self-hosters, unless the curator angle resonates more broadly.
- **Packaging:** a single Docker container with an Unraid Community Apps template is the expected distribution path.
