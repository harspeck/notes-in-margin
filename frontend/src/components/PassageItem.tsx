import { Link } from "react-router-dom";
import { clock } from "../lib/format";
import type { PassageSuggestion } from "../lib/types";

/** One passage the AI found for a question, as a row of a `ul.receipts`. With `onShow` the
 * passage is in the transcript already open, so it scrolls there; otherwise it links to it. */
export default function PassageItem({
  suggestion: s,
  busy,
  showConversation = true,
  onAccept,
  onDismiss,
  onShow,
}: {
  suggestion: PassageSuggestion;
  busy: boolean;
  showConversation?: boolean;
  onAccept: () => void;
  onDismiss: () => void;
  onShow?: () => void;
}) {
  const where = [
    s.speaker_name,
    s.time_seconds !== null ? clock(s.time_seconds) : "",
    showConversation ? s.conversation_title : "",
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <li className="found">
      "{s.text || "This passage's text couldn't be loaded."}"
      <div className="src">
        <span className="tag found">Passage the AI found</span>
        {where}
        {onShow ? (
          <button className="linkb" onClick={onShow}>
            Show in transcript
          </button>
        ) : (
          <Link className="linkb" to={`/read/${s.conversation_id}?snippet=${s.snippet_id}`}>
            Open in transcript
          </Link>
        )}
      </div>
      {s.reason && <p className="why">{s.reason}</p>}
      <div className="acts">
        <button className="btn small" onClick={onAccept} disabled={busy}>
          Add as a highlight
        </button>
        <button className="btn quiet small" onClick={onDismiss} disabled={busy}>
          Not this
        </button>
      </div>
    </li>
  );
}
