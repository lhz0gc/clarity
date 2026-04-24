# Clarity Architecture Plan

Solo founder: Longhao Zhang · Campus-first · B2B2C

---

## 1. Technical Architecture

### NOW (已实现)

**Frontend:** Single-file HTML/CSS/JS embedded in Python, PWA + Service Worker (v22), Canvas-based annotation engine, bilingual i18n (EN/中文), large text mode via CSS `--scale` variable.

**Signaling Server:** aiohttp + WebSocket, room management (4-person max), freeze state sync, Fly.io Singapore region.

**WebRTC:** Mesh topology (≤4 peers), Metered.ca TURN relay, ICE candidate exchange via signaling server. No recording, no SFU.

**Infrastructure:** Fly.io free tier (auto-scale, auto-stop), Cloudflare Worker reverse proxy at `longhaozhang.com/clarity`, Docker deployment, HTTPS via Fly.io.

### NEXT (下一步)

**Split Frontend Out:** Extract HTML/JS/CSS from Python into separate static files. Serve frontend via Cloudflare Pages (global CDN). Backend becomes pure API + WebSocket server. Benefit: faster load times, simpler iteration on UI.

**Analytics + Telemetry:** Lightweight event tracking (Plausible or self-hosted). Track session count, freeze usage rate, call duration, annotation saves. Critical for B-side pitch decks — you need data to prove value.

### LATER (长期)

**Own TURN Server:** Replace metered.ca dependency with self-hosted coturn on Fly.io or Hetzner. Lower latency in SEA region, no third-party rate limits.

---

## 2. Product Architecture

### NOW (已实现)

**Core Flow:** Open link/scan QR → Start call or enter room code → Video call (≤4 people) → Freeze → Annotate → Save → Resume or end.

**Accessibility:** Bilingual EN/中文, large text mode (1.25x), freeze coach mark (first-time tooltip), single-camera detection with toast, save nudge system (pulse animation + confirmation dialog).

**Distribution:** Web-only (no app store), PWA installable, QR code sharing, room code invite, zero-install zero-signup.

### NEXT (下一步)

**Guided Mode:** Pre-set annotation templates (arrows, circles, numbered steps). One-tap "point here" gesture. This is the key differentiator for campus navigation use cases — not just drawing, but guiding.

**Session History:** Auto-save annotated screenshots to local storage gallery. Share saved annotations. Creates "receipts" for guidance given — useful for both helpers and visitors.

### LATER (长期)

**B-Side Dashboard:** Admin panel for universities/venues. Custom QR codes with venue branding. Usage analytics per venue (sessions, peak times, satisfaction). This is what you charge for.

---

## 3. Business Architecture

### Model: B2B2C

- **C-end (students/visitors):** FREE forever. No signup, no install, no friction.
- **B-end (universities/venues):** PAY per event or per semester.
- **Value prop to B:** "Reduce helper headcount — one guide can help many visitors remotely via Clarity instead of stationing helpers at every building."

### Revenue Tiers (planned)

| Tier | Price | Includes |
|------|-------|----------|
| Free | $0 | ≤10 sessions/day, Clarity branding |
| Campus | $200/event | Custom QR codes, analytics dashboard, no branding |
| Enterprise | $500/month | White-label, API access, priority support |

Start with Campus tier at NUS FOP 2026.

### Go-to-Market: Campus First

1. **Phase 1 (May–Jul 2026):** Polish MVP, record demo video, pitch NUSSU/OSA/OCs for FOP orientation
2. **Phase 2 (Aug 2026):** Deploy at NUS FOP — first real users + usage data
3. **Phase 3 (Sep–Dec 2026):** Build analytics dashboard, pitch other NUS events + NTU/SMU/SIT
4. **Phase 4 (2027):** Enterprise tier, convention centers, hospitals, airports

### Competitive Moat

- **Network effect:** Campus guides build habit, recommend to other events
- **Switching cost:** University partnerships with custom QR infrastructure
- **Domain data:** Annotation templates tailored to specific venue types
- **Distribution advantage:** Zero-install web app beats any native app for one-time interactions

---

## 4. Solo Founder Playbook

1. **不招人:** Use AI for development, design, and copywriting. Keep burn rate = $0.
2. **不融资:** Infrastructure cost is near-zero (Fly.io + Cloudflare free tiers). No VC needed until proven traction.
3. **先证明:** One successful NUS pilot with real usage data is worth more than any pitch deck.

---

## Priority Order

```
1. Record 30-sec demo video of freeze+annotate flow
2. Pitch NUSSU/OSA for FOP 2026 pilot
3. Add Plausible analytics (proves value to B-side)
4. Build Guided Mode (annotation templates)
5. B-side dashboard (only after pilot proves demand)
```

---

*Last updated: 2026-04-24*
