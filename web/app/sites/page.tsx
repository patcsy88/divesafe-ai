import Link from "next/link";
import { redirect } from "next/navigation";
import Banner from "../../components/Banner.tsx";
import { ApiError, listSites } from "../../lib/api.ts";
import { requireKey } from "../../lib/auth.ts";
import { plain } from "../../lib/present.ts";
import type { SiteListView } from "../../lib/types.ts";

export default async function SitesPage() {
  const key = await requireKey();
  let list: SiteListView;
  try {
    list = await listSites(key);
  } catch (error) {
    if (error instanceof ApiError) {
      if (error.status === 401) redirect("/logout");
      return <p role="alert" className="error">{error.message}</p>;
    }
    throw error;
  }
  return (
    <>
      <header className="top">
        <h1>Dive sites</h1>
        <Link href="/">New assessment</Link>
      </header>
      <Banner />
      {list.sites.length === 0 && <p>No dive sites are registered.</p>}
      {list.sites.map((s) => (
        <section className="card" key={s.site.id}>
          <h2>{plain(s.site.name)}</h2>
          <dl>
            <dt>Id</dt>
            <dd>{plain(s.site.id)}</dd>
            <dt>Position</dt>
            <dd>{plain(String(s.site.latitude))}, {plain(String(s.site.longitude))} ({plain(s.site.coordinate_source)})</dd>
            {s.site.area_names.length > 0 && (
              <>
                <dt>Areas</dt>
                <dd>{s.site.area_names.map(plain).join(", ")}</dd>
              </>
            )}
          </dl>
          {s.site.constraints.length > 0 && (
            <>
              <h3>Registered constraints</h3>
              <ul>
                {s.site.constraints.map((c) => (
                  <li key={c.id}>
                    <span className="tag">{plain(c.threshold_status)}</span>
                    {plain(c.description)}
                    {c.threshold_status !== "VALIDATED" && " (a placeholder, not a limit)"}
                  </li>
                ))}
              </ul>
            </>
          )}
          <p className="hint">{plain(s.hazards_note)} Not listing a hazard does not mean there are none.</p>
        </section>
      ))}
      <p className="hint">{plain(list.notice)}</p>
    </>
  );
}
