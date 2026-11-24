// The localization field exists once per record, but every annotator marks it
// independently. Each annotator therefore owns one entry under
// `fields[field].annotators[<user id>]`, and the adapter only ever reads and
// writes the entry of the signed-in user.
class Adapter {
  #storage;
  #record;
  #field;
  #user = null;
  #inflight = false;
  #dirty = null;

  constructor(storage, record, field) {
    this.#storage = storage;
    this.#record = record;
    this.#field = field;
  }

  get #token() {
    return this.#storage.getItem("auth._token.local");
  }

  get #key() {
    return `${this.#field}:${this.#record.id}:${this.#user}`;
  }

  async #request(method, body) {
    const response = await fetch(`/api/v1/records/${this.#record.id}`, {
      method,
      headers: {
        Authorization: this.#token,
        "Content-Type": "application/json",
      },
      body: body && JSON.stringify(body),
    });
    if (!response.ok) {
      throw new Error(`${method} failed (${response.status})`);
    }
    return response.json();
  }

  async identify() {
    const response = await fetch("/api/v1/me", {
      headers: { Authorization: this.#token },
    });
    if (!response.ok) {
      throw new Error(`Sign-in check failed (${response.status})`);
    }
    this.#user = (await response.json()).id;
  }

  async save(selection) {
    this.#inflight = true;
    const entry = {
      state: selection.state,
      annotation: selection.annotation,
      timestamp: Date.now(),
    };
    this.#storage.setItem(this.#key, JSON.stringify(entry));

    try {
      // Re-read the record right before writing so entries other annotators
      // saved since this page loaded are carried over instead of overwritten.
      // PATCH /records replaces the whole fields dict, it does not merge, so
      // every field is resent and only this user's entry is replaced.
      const { fields } = await this.#request("GET");
      const current = fields[this.#field];
      await this.#request("PATCH", {
        fields: {
          ...fields,
          [this.#field]: {
            url: current.url,
            annotators: { ...current.annotators, [this.#user]: entry },
          },
        },
      });
      this.#storage.removeItem(this.#key);
      element.status("info", "Saved");
    } catch (error) {
      // fetch rejects with a TypeError only when the server is unreachable.
      if (error instanceof TypeError) {
        element.status("warning", "Saved locally");
      } else {
        element.status("error", `Save failed: ${error.message}`);
      }
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

  // A local entry only survives a save that never reached the server. It wins
  // over the server entry when it is newer; both timestamps come from this
  // user's own browser, so other annotators' saves never discard it.
  restore() {
    const server = this.#record.fields[this.#field].annotators?.[this.#user] ?? null;
    const raw = this.#storage.getItem(this.#key);

    if (!raw) {
      return server;
    }

    const local = JSON.parse(raw);
    if (local.timestamp > (server?.timestamp ?? 0)) {
      return local;
    }

    this.#storage.removeItem(this.#key);
    return server;
  }
}

// Some collected repositories are no longer on GitHub, so the record page opens
// GitHub links in the repository mirror instead. Only the links on the page are
// rewritten; the record fields keep the original URLs, and the adapter reads
// the record through the API, never from the page.
class Mirror {
  #origin;

  constructor(origin) {
    this.#origin = origin;
  }

  rewrite(root) {
    for (const anchor of root.querySelectorAll('a[href^="https://github.com/"]')) {
      anchor.href = this.#origin + anchor.getAttribute("href").slice("https://github.com".length);
    }
  }

  // Argilla renders the record fields outside this iframe and replaces them on
  // every record change, so the parent page is rewritten again on each change.
  watch(page) {
    this.rewrite(page);
    new MutationObserver(() => this.rewrite(page)).observe(page.body, {
      childList: true,
      subtree: true,
    });
  }
}

// `record` is the global Argilla injects into the custom field iframe; it is
// referenced only here and handed to the adapter, which stays free of globals.
const element = document.querySelector("code-annotation");
const field = element.getAttribute("field");
const adapter = new Adapter(parent.localStorage, record, field);

element.setAttribute("src", record.fields[field].url);
new Mirror("https://mirror.bug-request.dev").watch(parent.document);

adapter.identify().then(
  () => {
    const restored = adapter.restore();
    if (restored) {
      element.value = { state: restored.state, annotation: restored.annotation ?? [] };
    }
    element.addEventListener("change", (e) => adapter.push(e.detail));
  },
  (error) => element.status("error", error.message),
);
