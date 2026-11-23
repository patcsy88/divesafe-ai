// Runs once when the server starts. A missing session secret or a bad API address stops the
// server here, instead of showing as a failure on every request.
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    const { checkStartup } = await import("./lib/startup.ts");
    checkStartup();
  }
}
