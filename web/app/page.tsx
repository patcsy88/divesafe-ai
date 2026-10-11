import Banner from "../components/Banner.tsx";
import AssessmentForm from "../components/AssessmentForm.tsx";
import { logout } from "./actions.ts";
import { requireKey } from "../lib/auth.ts";

export default async function Home() {
  await requireKey();
  return (
    <>
      <header className="top">
        <h1>New assessment</h1>
        <form action={logout}>
          <button type="submit" className="link">Sign out</button>
        </form>
      </header>
      <Banner />
      <AssessmentForm />
    </>
  );
}
