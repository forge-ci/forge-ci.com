import fragmentSource from "./pulse-test1.glsl?raw";

const vertexSource = `#version 300 es
void main() {
  vec2 corner = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(corner * 2.0 - 1.0, 0.0, 1.0);
}`;

function shader(gl: WebGL2RenderingContext, type: number, source: string): WebGLShader | null {
  const compiled = gl.createShader(type);
  if (compiled === null) return null;
  gl.shaderSource(compiled, source);
  gl.compileShader(compiled);
  if (gl.getShaderParameter(compiled, gl.COMPILE_STATUS) === true) return compiled;
  gl.deleteShader(compiled);
  return null;
}

function drawBackground(canvas: HTMLCanvasElement): void {
  const gl = canvas.getContext("webgl2", { alpha: false, antialias: false, depth: false });
  if (gl === null) return;
  const vertex = shader(gl, gl.VERTEX_SHADER, vertexSource);
  const fragment = shader(gl, gl.FRAGMENT_SHADER, fragmentSource);
  const program = gl.createProgram() as WebGLProgram | null;
  if (vertex === null || fragment === null || program === null) return;
  gl.attachShader(program, vertex);
  gl.attachShader(program, fragment);
  gl.linkProgram(program);
  gl.deleteShader(vertex);
  gl.deleteShader(fragment);
  if (gl.getProgramParameter(program, gl.LINK_STATUS) !== true) {
    gl.deleteProgram(program);
    return;
  }
  gl.useProgram(program);
  const resolution = gl.getUniformLocation(program, "u_resolution");
  const time = gl.getUniformLocation(program, "u_time");
  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  const started = performance.now();
  let frame = 0;

  const draw = (now: number): void => {
    const scale = Math.min(1, 1440 / Math.max(canvas.clientWidth, canvas.clientHeight));
    const width = Math.max(1, Math.round(canvas.clientWidth * scale));
    const height = Math.max(1, Math.round(canvas.clientHeight * scale));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    gl.viewport(0, 0, width, height);
    gl.uniform2f(resolution, width, height);
    gl.uniform1f(time, motion.matches ? 0 : (now - started) / 1000);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    canvas.classList.add("is-rendered");
    if (!motion.matches) frame = requestAnimationFrame(draw);
  };

  const refresh = (): void => {
    cancelAnimationFrame(frame);
    if (!document.hidden && !gl.isContextLost()) draw(performance.now());
  };

  canvas.addEventListener("webglcontextlost", () => {
    cancelAnimationFrame(frame);
    canvas.classList.remove("is-rendered");
  });
  document.addEventListener("visibilitychange", refresh);
  motion.addEventListener("change", refresh);
  window.addEventListener("resize", refresh);
  refresh();
}

const background = document.querySelector("canvas");
if (background instanceof HTMLCanvasElement) drawBackground(background);
