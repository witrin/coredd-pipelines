class Adapter {
  #storage;
  #record;
  #field;
  #key;
  #inflight = false;
  #dirty = null;

  constructor(storage, record, field) {
    this.#storage = storage;
    this.#record = record;
    this.#field = field;
    this.#key = `${field}:${record.id}`;
  }

  get #token() {
    return this.#storage.getItem("auth._token.local");
  }

  async save(selection) {
    this.#inflight = true;
    this.#storage.setItem(this.#key, JSON.stringify({
      selection,
      timestamp: Date.now(),
    }));

    try {
      const response = await fetch(`/api/v1/records/${this.#record.id}`, {
        method: "PATCH",
        headers: {
          Authorization: this.#token,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          // PATCH /records replaces the whole fields dict, it does not merge.
          // Resend every field, mutating only annotation, or the server rejects
          // the request with 422 "missing required value for field: ...".
          // `state` is the raw checkpoint restored verbatim on reload; the
          // `annotation` projection is what the backend consumes.
          fields: {
            ...this.#record.fields,
            [this.#field]: {
              url: this.#record.fields[this.#field].url,
              state: selection.state,
              annotation: selection.annotation,
            },
          },
        }),
      });

      if (response.ok) {
        this.#storage.removeItem(this.#key);
        element.status("info", "Saved");
      } else {
        element.status("error", `Save failed (${response.status})`);
      }
    } catch {
      element.status("warning", "Saved locally");
    } finally {
      this.#inflight = false;
      if (this.#dirty) {
        const next = this.#dirty;
        this.#dirty = null;
        this.save(next);
      }
    }
  }

  push(selection) {
    if (this.#inflight) {
      this.#dirty = selection;
    } else {
      this.save(selection);
    }
  }

  restore() {
    const stored = this.#record.fields[this.#field];
    const server = stored.state
      ? { state: stored.state, annotation: stored.annotation ?? [] }
      : null;
    const raw = this.#storage.getItem(this.#key);

    if (!raw) {
      return server;
    }

    const { selection, timestamp } = JSON.parse(raw);
    const updated = new Date(this.#record.updated_at).getTime();

    if (timestamp > updated) {
      return selection;
    }

    this.#storage.removeItem(this.#key);
    return server;
  }
}

// `record` is the global Argilla injects into the custom field iframe; it is
// referenced only here and handed to the adapter, which stays free of globals.
const element = document.querySelector("code-annotation");
const field = element.getAttribute("field");
const adapter = new Adapter(parent.localStorage, record, field);

element.setAttribute("src", record.fields[field].url);

const restored = adapter.restore();
if (restored) {
  element.value = restored;
}

element.addEventListener("change", (e) => adapter.push(e.detail));
