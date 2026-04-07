/**
 * Half-open line range [start, end) where start is inclusive and end is exclusive, 1-based.
 */
export type Interval = [number, number];

/**
 * The kind of change a file underwent in a commit.
 */
export type Action = "add" | "delete" | "change" | "unchanged";

/**
 * A file change entry with an explicit action.
 */
export interface Change {
  action: Action;
  source?: string;
  target?: string;
}

/**
 * A commit as represented in the input JSON.
 */
export interface Commit {
  sha: string;
  author: string;
  message: string;
  time: string;
  labels: string[];
  changes: Record<string, string | Change>;
}

/**
 * A target commit as represented in the input JSON.
 * `annotations` is optional — when absent or empty the target has no suggestions.
 */
export interface Target extends Commit {
  annotations?: { path: string; lines: Interval[]; description: string }[];
}

/**
 * The top-level data structure loaded from the JSON URL.
 * `targets` is optional — when absent or empty the component becomes a read-only commit viewer.
 */
export interface Data {
  repository: string;
  model: string;
  context: Commit;
  targets?: Target[];
}

/**
 * The origin of an annotation: model-generated suggestion or annotator-created.
 */
export type Origin = "suggestion" | "manual";

/**
 * The lifecycle status of an annotation.
 * blocked: suggestion suppressed by an overlapping manual (actions hidden, no output contribution).
 */
export type Status =
  | "pending"
  | "accepted"
  | "rejected"
  | "blocked"
  | "absorbed";

/**
 * A single entry in the change event output.
 */
export interface Entry {
  sha: string;
  path: string;
  lines: Interval[];
}

/**
 * A single annotation in the runtime state.
 */
export interface Annotation {
  identifier: string;
  sha: string;
  path: string;
  lines: Interval[];
  origin: Origin;
  status: Status;
  description: string;
}

/**
 * Severity of a status notification.
 */
export type Level = "info" | "warning" | "error";

/**
 * A single status notification shown to the user.
 */
export interface Notice {
  identifier: string;
  level: Level;
  message: string;
}
