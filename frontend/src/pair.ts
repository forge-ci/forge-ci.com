import "./splash";

const code = new URLSearchParams(location.hash.slice(1)).get("code");
const input = document.querySelector("#code");

if (code !== null && /^[A-Z2-9]{5}-[A-Z2-9]{5}$/i.test(code) && input instanceof HTMLInputElement) {
  input.value = code.toUpperCase();
  history.replaceState(history.state, "", location.pathname + location.search);
  const help = document.querySelector("#code-help");
  if (help !== null) help.textContent = "Code filled in. Continue with GitHub to finish.";
}
