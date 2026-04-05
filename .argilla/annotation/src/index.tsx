import React from "react";
import { createRoot } from "react-dom/client";

class AnnotationView extends HTMLElement {
  private root: ReturnType<typeof createRoot> | null = null;

  connectedCallback(): void {
    const shadow = this.attachShadow({ mode: "open" });
    const container = document.createElement("div");
    shadow.appendChild(container);
    this.root = createRoot(container);
    this.root.render(<Placeholder src={this.getAttribute("src") ?? ""} />);
  }

  disconnectedCallback(): void {
    this.root?.unmount();
    this.root = null;
  }
}

function Placeholder({ src }: { src: string }): React.ReactElement {
  return (
    <div style={{ fontFamily: "monospace", padding: "1rem", color: "#a0a3ab" }}>
      code-annotation {src ? `— ${src}` : "(no src)"}
    </div>
  );
}

customElements.define("code-annotation", AnnotationView);
