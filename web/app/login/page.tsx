import { redirect } from "next/navigation";
import Banner from "../../components/Banner.tsx";
import LoginForm from "../../components/LoginForm.tsx";
import { readKey } from "../../lib/auth.ts";

export default async function LoginPage() {
  if (await readKey()) redirect("/");
  return (
    <>
      <h1>DiveSafe AI</h1>
      <Banner />
      <LoginForm />
    </>
  );
}
