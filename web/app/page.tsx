import Link from "next/link";
import Banner from "../components/Banner.tsx";
import AssessmentForm from "../components/AssessmentForm.tsx";
import { logout } from "./actions.ts";
import { ApiError, listSites } from "../lib/api.ts";
import { requireKey } from "../lib/auth.ts";
import { redirect } from "next/navigation";

export default async function Home() {
  const key = await requireKey();
  let sites: { id: string; label: string }[] = [];
  let problem: string | null = null;
  try {
    const list = await listSites(key);
    sites = list.sites.map((s) => ({ id: s.site.id, label: s.site.name }));
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) redirect("/logout");
    problem = error instanceof ApiError ? error.message : "Could not load the sites.";
  }
  return (
    <>
      <header className="top">
        <h1>New assessment</h1>
        <form action={logout}>
          <button type="submit" className="link">Sign out</button>
        </form>
      </header>
      <Banner />
      <nav className="sub"><Link href="/sites">Dive sites</Link></nav>
      {problem && <p role="alert" className="error">{problem}</p>}
      {sites.length > 0 ? (
        <AssessmentForm sites={sites} />
      ) : (
        !problem && <p>No dive sites are registered, so an assessment cannot be run.</p>
      )}
    </>
  );
}
