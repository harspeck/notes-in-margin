import { useImperativeHandle, useRef, useState } from "react";
import type { Ref } from "react";
import { clock } from "../lib/format";
import { Icon } from "../lib/ui";

const SPEEDS = [0.75, 1, 1.25, 1.5, 2];
const SKIP_SECONDS = 10;
const PLAY_FAILED = "This conversation's audio could not be played.";

export interface PlayerHandle {
  /** Jumps to a time and starts playing. */
  playFrom: (seconds: number) => void;
  /** Jumps to a time and leaves play or pause as it was. */
  moveTo: (seconds: number) => void;
}

interface Props {
  ref: Ref<PlayerHandle>;
  src: string;
  /** Cortico's length for the conversation. The audio file's own length is used without it. */
  durationSeconds: number | null;
  /** The audio time, each time it moves. */
  onTime: (seconds: number) => void;
  onEnded: () => void;
  onError: (message: string) => void;
}

/** Keeps the audio time in its own state, so the transcript is not rendered again on every tick. */
export default function Player({ ref, src, durationSeconds, onTime, onEnded, onError }: Props) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [position, setPosition] = useState(0);
  const [speed, setSpeed] = useState(1);
  const [fileDuration, setFileDuration] = useState<number | null>(null);
  const duration = durationSeconds ?? fileDuration ?? 0;

  function play() {
    // Pausing while the audio is still loading rejects the play with AbortError: not a failure.
    audioRef.current?.play().catch((e) => {
      if ((e as { name?: string } | null)?.name !== "AbortError") onError(PLAY_FAILED);
    });
  }

  function moveTo(seconds: number) {
    const audio = audioRef.current;
    if (!audio) return;
    const to = Math.max(0, duration ? Math.min(seconds, duration) : seconds);
    audio.currentTime = to;
    // Nothing is loaded until the first play, so no time event follows a move made before it.
    setPosition(to);
    onTime(to);
  }

  useImperativeHandle(ref, () => ({
    moveTo,
    playFrom: (seconds: number) => {
      moveTo(seconds);
      play();
    },
  }));

  return (
    <div className="player" role="group" aria-label="Conversation audio">
      <audio
        ref={audioRef}
        src={src}
        preload="none"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onDurationChange={(e) => {
          if (Number.isFinite(e.currentTarget.duration)) setFileDuration(e.currentTarget.duration);
        }}
        onTimeUpdate={(e) => {
          setPosition(e.currentTarget.currentTime);
          onTime(e.currentTarget.currentTime);
        }}
        onEnded={onEnded}
        onError={() => onError(PLAY_FAILED)}
      />
      <button className="skip" onClick={() => moveTo(position - SKIP_SECONDS)} aria-label={`Back ${SKIP_SECONDS} seconds`}>
        −{SKIP_SECONDS}
      </button>
      <button
        className="pp"
        onClick={() => (audioRef.current?.paused ? play() : audioRef.current?.pause())}
        aria-label={playing ? "Pause" : "Play"}
      >
        <Icon name={playing ? "pause" : "play"} fill />
      </button>
      <button
        className="skip"
        onClick={() => moveTo(position + SKIP_SECONDS)}
        aria-label={`Forward ${SKIP_SECONDS} seconds`}
      >
        +{SKIP_SECONDS}
      </button>
      <span>{clock(position)}</span>
      <input
        className="scrub"
        type="range"
        min={0}
        max={Math.max(1, Math.floor(duration))}
        step={1}
        value={Math.floor(position)}
        onChange={(e) => moveTo(Number(e.target.value))}
        aria-label="Position in the conversation"
        aria-valuetext={`${clock(position)} of ${clock(duration)}`}
        disabled={!duration}
      />
      <span>{clock(duration)}</span>
      <select
        aria-label="Playback speed"
        value={speed}
        onChange={(e) => {
          const next = Number(e.target.value);
          setSpeed(next);
          if (audioRef.current) audioRef.current.playbackRate = next;
        }}
      >
        {SPEEDS.map((s) => (
          <option key={s} value={s}>
            {s}x
          </option>
        ))}
      </select>
    </div>
  );
}
