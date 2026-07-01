class Adapter {
  #storage;
  #record = record;
  #key = `annotation:${record.id}`;
  #inflight = false;
  #dirty = null;

  constructor(storage) {
    this.#storage = storage;
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
            annotation: {
              url: this.#record.fields.annotation.url,
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
    const field = this.#record.fields.annotation;
    const server = field.state
      ? { state: field.state, annotation: field.annotation ?? [] }
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

const adapter = new Adapter(parent.localStorage);
const element = document.querySelector("code-annotation");

element.setAttribute("src", record.fields.annotation.url);

const restored = adapter.restore();
if (restored) {
  element.value = restored;
}

element.addEventListener("change", (e) => adapter.push(e.detail));
