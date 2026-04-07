import type { Level, Notice } from "../types/index.js";

/**
 * Manages a stack of status notifications with auto-dismiss for info level.
 * Follows the same subscribe/snapshot pattern as Selection for useSyncExternalStore.
 */
export class Toasts {
  #items: Notice[] = [];
  #timers: Map<string, number> = new Map();
  #listeners: Set<() => void> = new Set();
  #version = 0;
  #counter = 0;

  /**
   * Adds a notice and notifies subscribers.
   * Info-level notices auto-dismiss after 4 seconds.
   * Caps visible notices at 5, evicting the oldest.
   */
  push(level: Level, message: string): void {
    const identifier = String(++this.#counter);
    const notice: Notice = { identifier, level, message };
    this.#items = [...this.#items, notice];
    if (this.#items.length > 5) {
      const evicted = this.#items[0];
      const timer = this.#timers.get(evicted.identifier);
      if (timer !== undefined) {
        clearTimeout(timer);
        this.#timers.delete(evicted.identifier);
      }
      window.setTimeout(() => this.dismiss(evicted.identifier), 250);
    }
    if (level === "info") {
      const timer = window.setTimeout(() => {
        this.dismiss(identifier);
      }, 4000);
      this.#timers.set(identifier, timer);
    }
    this.#notify();
  }

  /**
   * Removes a notice by identifier and clears its timer.
   */
  dismiss(identifier: string): void {
    const before = this.#items.length;
    this.#items = this.#items.filter((n) => n.identifier !== identifier);
    if (this.#items.length === before) return;
    const timer = this.#timers.get(identifier);
    if (timer !== undefined) {
      clearTimeout(timer);
      this.#timers.delete(identifier);
    }
    this.#notify();
  }

  /**
   * Returns the current list of visible notices.
   */
  project(): Notice[] {
    return this.#items;
  }

  /**
   * Subscribes to state changes. Returns an unsubscribe function.
   */
  subscribe(listener: () => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  /**
   * Returns the current version. Changes on every write.
   */
  snapshot(): number {
    return this.#version;
  }

  /**
   * Clears all pending timers. Call on unmount.
   */
  dispose(): void {
    for (const timer of this.#timers.values()) clearTimeout(timer);
    this.#timers.clear();
  }

  #notify(): void {
    this.#version++;
    for (const listener of this.#listeners) listener();
  }
}
