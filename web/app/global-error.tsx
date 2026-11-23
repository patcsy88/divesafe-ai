"use client";

// Shown when the root layout itself fails. It replaces the whole document, so it has its own
// <html> and <body>. Fixed text only: no error detail reaches the page.
export default function GlobalError() {
  return (
    <html lang="en">
      <body>
        <main>
          <h1>No result</h1>
          <p role="alert">
            NO RESULT. This page failed to load. Do not act on it. Reload the page, or ask the
            decider.
          </p>
        </main>
      </body>
    </html>
  );
}
