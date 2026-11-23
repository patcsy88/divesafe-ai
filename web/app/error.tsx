"use client";

// Shown when a page fails while rendering. Fixed text only: no error detail reaches the page.
export default function ErrorPage() {
  return (
    <section className="ds-card">
      <h1>No result</h1>
      <p role="alert" className="ds-error">
        NO RESULT. This page failed to load. Do not act on it. Reload the page, or ask the decider.
      </p>
    </section>
  );
}
