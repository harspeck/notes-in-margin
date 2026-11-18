import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, errorText } from "../lib/api";
import type { NextStepSuggestion } from "../lib/types";
import { AiMark } from "../lib/ui";

/** The conversation recommended once a session ends. It asks as soon as it is shown; the
 * server answers from what is already suggested when it can, and only then asks the AI. */
export default function ListenNext() {
  const navigate = useNavigate();
  const [state, setState] = useState<"asking" | "done">("asking");
  const [pick, setPick] = useState<NextStepSuggestion | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .recommendNext()
      .then((r) => live && setPick(r.recommendation))
      .catch((e) => live && setError(errorText(e)))
      .finally(() => live && setState("done"));
    return () => {
      live = false;
    };
  }, []);

  if (state === "asking")
    return (
      <p className="hint listennext" role="status">
        <AiMark /> Finding a conversation to read next…
      </p>
    );
  if (error) return <p className="hint listennext">Couldn't suggest a next conversation. {error}</p>;
  if (!pick || pick.recommended_conversation_id === null) return null;
  const conversationId = pick.recommended_conversation_id;

  return (
    <section className="card ai lcard listennext" aria-label="Read next">
      <div className="t">
        <AiMark /> Read next: {pick.recommended_conversation_title ?? "a conversation"}
      </div>
      <p className="why">{pick.rationale}</p>
      <div className="acts">
        <button
          className="btn small"
          onClick={async () => {
            await api.acceptNextStep(pick.id).catch(() => {});
            navigate(`/read/${conversationId}`);
          }}
        >
          Open it
        </button>
        <button
          className="btn quiet small"
          onClick={async () => {
            await api.dismissNextStep(pick.id).catch(() => {});
            setPick(null);
          }}
        >
          Not this one
        </button>
      </div>
    </section>
  );
}
