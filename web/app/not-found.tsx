import Link from "next/link";

export default function NotFound() {
  return (
    <section className="ds-card">
      <h1>Not found</h1>
      <p>That assessment does not exist, or you cannot see it.</p>
      <Link href="/">New assessment</Link>
    </section>
  );
}
