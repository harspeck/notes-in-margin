import { useEffect, useState } from "react";
import { api } from "../lib/api";

export default function SettingsPage() {
  const [text, setText] = useState("");
  const [defaultText, setDefaultText] = useState("");
  const [savedMessage, setSavedMessage] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api.getReviewPrompt().then((r) => {
      setText(r.text);
      setDefaultText(r.default_text);
      setLoaded(true);
    });
  }, []);

  async function save() {
    // Before the prompt has loaded the box is empty, and saving would store an empty prompt.
    if (!loaded) return;
    setSavedMessage(null);
    try {
      await api.saveReviewPrompt(text);
      setSavedMessage("Saved. The next review will use this prompt.");
    } catch {
      setSavedMessage("Couldn't save. Try again.");
    }
  }

  async function reset() {
    const r = await api.resetReviewPrompt();
    setText(r.text);
    setSavedMessage("Reset to default.");
  }

  return (
    <div style={{ maxWidth: 700, margin: "0 auto", padding: "2rem", fontFamily: "sans-serif" }}>
      <h1>AI review prompt</h1>
      <p style={{ color: "#666" }}>
        This is sent to Claude alongside what you said in your reflection, your
        research questions, the transcript, and your highlights. Edit it and the next review will use your version.
      </p>
      <textarea aria-label="Review prompt" rows={16} style={{ width: "100%", fontFamily: "monospace" }} value={text} onChange={(e) => setText(e.target.value)} />
      <div style={{ marginTop: "0.75rem", display: "flex", gap: "0.5rem" }}>
        <button className="primary" onClick={save}>
          Save
        </button>
        <button className="secondary" onClick={reset}>
          Reset to default
        </button>
      </div>
      {savedMessage && <p style={{ color: "#2a7a2a" }}>{savedMessage}</p>}
      <details style={{ marginTop: "1rem" }}>
        <summary>Default prompt</summary>
        <pre style={{ whiteSpace: "pre-wrap" }}>{defaultText}</pre>
      </details>
    </div>
  );
}
