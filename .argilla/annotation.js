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

  async save(selections) {
    this.#inflight = true;
    this.#storage.setItem(this.#key, JSON.stringify({
      selections,
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
          fields: {
            ...this.#record.fields,
            annotation: {
              url: this.#record.fields.annotation.url,
              selections,
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

  push(selections) {
    if (this.#inflight) {
      this.#dirty = selections;
    } else {
      this.save(selections);
    }
  }

  restore() {
    const server = this.#record.fields.annotation.selections ?? null;
    const raw = this.#storage.getItem(this.#key);

    if (!raw) {
      return server;
    }

    const { selections, timestamp } = JSON.parse(raw);
    const updated = new Date(this.#record.updated_at).getTime();

    if (timestamp > updated) {
      return selections;
    }

    this.#storage.removeItem(this.#key);
    return server;
  }
}

const adapter = new Adapter(parent.localStorage);
const element = document.querySelector("code-annotation");

element.setAttribute("src", record.fields.annotation.url);

const selections = adapter.restore();
if (selections) {
  element.value = selections;
}

element.addEventListener("change", (e) => adapter.push(e.detail));
