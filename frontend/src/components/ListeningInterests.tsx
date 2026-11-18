import { useEffect, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { api, errorText } from "../lib/api";
import { useToast } from "../lib/format";
import type { ListeningInterest, ListeningSuggestion } from "../lib/types";
import { AiMark, Icon } from "../lib/ui";

const LABEL = "Something you want to hear more about";
const MAX_CHARS = 500;

/** The "what you want to hear next" lines: each is edited in place and saved when it loses
 * focus. `children` sit beside "Add a line". */
export default function ListeningInterests({ children }: { children?: ReactNode }) {
  const toast = useToast();
  const [lines, setLines] = useState<ListeningInterest[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [offers, setOffers] = useState<ListeningSuggestion[]>([]);
  const [thinking, setThinking] = useState(false);

  useEffect(() => {
    let live = true;
    api
      .listListeningInterests()
      .then(setLines)
      .catch((e) => setError(errorText(e)));
    // Offers saved earlier show at once. Then the server is asked for new ones: it only
    // goes to the AI when the questions or highlights have changed since it last did.
    api
      .listListeningSuggestions()
      .then((saved) => {
        if (!live) return;
        setOffers(saved);
        if (saved.length > 0) return;
        setThinking(true);
        return api.refreshListeningSuggestions().then((r) => live && setOffers(r.suggestions));
      })
      // The list works without offers, so a failure here is not shown.
      .catch(() => {})
      .finally(() => live && setThinking(false));
    return () => {
      live = false;
    };
  }, []);

  async function keep(offer: ListeningSuggestion) {
    try {
      const { interest } = await api.acceptListeningSuggestion(offer.id);
      setOffers((prev) => prev.filter((o) => o.id !== offer.id));
      setLines((prev) => [...(prev ?? []), interest]);
      setError(null);
      toast("Added to your list.");
    } catch (e) {
      setError(errorText(e));
    }
  }

  async function turnDown(offer: ListeningSuggestion) {
    try {
      await api.dismissListeningSuggestion(offer.id);
      setOffers((prev) => prev.filter((o) => o.id !== offer.id));
      setError(null);
      toast("Not added. It won't be offered again.");
    } catch (e) {
      setError(errorText(e));
    }
  }

  async function save(line: ListeningInterest, input: HTMLInputElement) {
    const text = input.value.trim();
    if (!text || text === line.text) {
      input.value = line.text;
      return;
    }
    try {
      const updated = await api.editListeningInterest(line.id, text);
      setLines((prev) => (prev ?? []).map((l) => (l.id === line.id ? updated : l)));
      setError(null);
      toast("Saved.");
    } catch (e) {
      input.value = line.text;
      setError(errorText(e));
    }
  }

  async function add(text: string) {
    setAdding(false);
    if (!text.trim()) return;
    try {
      const created = await api.addListeningInterest(text.trim());
      setLines((prev) => [...(prev ?? []), created]);
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }

  async function remove(line: ListeningInterest) {
    try {
      await api.removeListeningInterest(line.id);
      setLines((prev) => (prev ?? []).filter((l) => l.id !== line.id));
      setError(null);
      toast("Removed. It won't steer the next suggestions.");
    } catch (e) {
      setError(errorText(e));
    }
  }

  const blurOnEnter = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") e.currentTarget.blur();
  };

  return (
    <>
      <p className="hint" style={{ margin: "-4px 0 12px" }}>
        What steers the conversations suggested to you. Lines come from your reflections, and the AI offers more from
        your questions and recent highlights. Edit any of them.
      </p>
      {error && <p className="err" role="alert">{error}</p>}
      {lines === null && !error && <p className="hint">Loading…</p>}
      {lines?.length === 0 && !adding && offers.length === 0 && !thinking && (
        <p className="hint" style={{ marginBottom: 12 }}>
          No lines yet. They appear after you reflect, jot a question or highlight something. Or add one yourself.
        </p>
      )}
      <ul className="wants">
        {lines?.map((line) => (
          <li key={line.id}>
            <input
              className="inp"
              defaultValue={line.text}
              maxLength={MAX_CHARS}
              aria-label={LABEL}
              onBlur={(e) => save(line, e.currentTarget)}
              onKeyDown={blurOnEnter}
            />
            <button className="btn quiet small" onClick={() => remove(line)} aria-label={`Remove "${line.text}"`}>
              <Icon name="x" />
            </button>
          </li>
        ))}
        {offers.map((offer) => (
          <li className="offer" key={`offer-${offer.id}`} data-offer={offer.id}>
            <AiMark label="Offered by AI" />
            <span className="txt">
              {offer.text}
              {offer.reason && <small>{offer.reason}</small>}
            </span>
            <button className="btn small" onClick={() => keep(offer)} aria-label={`Keep "${offer.text}"`}>
              Keep
            </button>
            <button className="btn quiet small" onClick={() => turnDown(offer)} aria-label={`Not this: "${offer.text}"`}>
              Not this
            </button>
          </li>
        ))}
        {thinking && (
          <li className="hint" role="status">
            <AiMark /> Looking at your questions and highlights…
          </li>
        )}
        {adding && (
          <li>
            <input
              className="inp"
              autoFocus
              maxLength={MAX_CHARS}
              placeholder={LABEL}
              aria-label={LABEL}
              onBlur={(e) => add(e.currentTarget.value)}
              onKeyDown={blurOnEnter}
            />
          </li>
        )}
      </ul>
      <div className="acts">
        <button className="btn outline small" onClick={() => setAdding(true)} disabled={adding || lines === null}>
          Add a line
        </button>
        {children}
      </div>
    </>
  );
}
