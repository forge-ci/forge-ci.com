#version 300 es
/*
Pulse Test1 — https://fragcoord.xyz/s/p0385h9e
Copyright (c) 2026 @yli110

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/
precision highp float;
uniform vec2 u_resolution;
uniform float u_time;
out vec4 fragColor;

// SPDX-License-Identifier: MIT
// Copyright (c) 2026 @yli110
//[LICENSE] https://opensource.org/licenses/MIT

#define R u_resolution.xy
#define PIXEL 10.0/min(R.x,R.y)
#define S smoothstep
#define T u_time

#define L(p,b) length( p - (b) * clamp( dot(p,b)/dot(b,b), 0., 1. ) )

const float a[3] = float[](0.,1.,-1.);

float hash(vec2 p) {
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}

float cn(vec2 a, vec2 d) {
    float n = hash(a);

    vec2 c = normalize(a), l = normalize(d);
    //Alignment
    float m = abs(dot(c, l)),
    //Probability
    p = S(.7, .98, m)*.75, t = length(a),
    //Central concentration
    ce = 1.0 - S(0.0, 2., t);
    //The farther the branch, the lower the probability.
    p *= S(18.0, 2.0, t);
    p = mix(p, 0.8, ce);

    return mix(1e2,0.,step(n, p));
}

void main() {
  vec2 F = gl_FragCoord.xy;

    vec2 uv = (F - 0.5 * R) / R.y;

    float s = 20., d = 1e2, m = 1.;
    vec2 u = uv * s,
        i = floor(u),
        f = fract(u) - .5;

    // Eight directions and their axis
    // draw line
    for(int m = 1; m < 9; ++m)
    {
        int j = m%3, k = m/3;
        vec2 q = vec2(a[j], a[k]), p = i + q*.5;
        d = min(d, cn(p, q)+L(f, q*.5));
    }
    /*
    // Eight directions and their axis
    vec2 pT = id + vec2(0.0, 0.5);  vec2 aT = vec2(0.0, 1.0);
    vec2 pB = id + vec2(0.0, -0.5); vec2 aB = vec2(0.0, 1.0);
    vec2 pR = id + vec2(0.5, 0.0);  vec2 aR = vec2(1.0, 0.0);
    vec2 pL = id + vec2(-0.5, 0.0); vec2 aL = vec2(1.0, 0.0);

    vec2 pTR = id + vec2(0.5, 0.5);   vec2 aTR = vec2(1.0, 1.0);
    vec2 pBL = id + vec2(-0.5, -0.5); vec2 aBL = vec2(1.0, 1.0);
    vec2 pTL = id + vec2(-0.5, 0.5);  vec2 aTL = vec2(-1.0, 1.0);
    vec2 pBR = id + vec2(0.5, -0.5);  vec2 aBR = vec2(-1.0, 1.0);

    float d = 100.0;

    // draw line
    d = min(d, connect(pT, aT)+L(guv, vec2(0.0, 0.5)));
    d = min(d, connect(pB, aB)+L(guv, vec2(0.0, -0.5)));
    d = min(d, connect(pR, aR)+L(guv, vec2(0.5, 0.0)));
    d = min(d, connect(pL, aL)+L(guv, vec2(-0.5, 0.0)));

    d = min(d, connect(pTR, aTR)+L(guv, vec2(0.5, 0.5)));
    d = min(d, connect(pBL, aBL)+L(guv, vec2(-0.5, -0.5)));
    d = min(d, connect(pTL, aTL)+L(guv, vec2(-0.5, 0.5)));
    d = min(d, connect(pBR, aBR)+L(guv, vec2(0.5, -0.5)));
    */

    //Color

    float t = 0.1, an = atan(i.y, i.x), di = length(uv)*5.,
    //Border
    l = S(t + PIXEL, t, d) - S(t, 0., d),
    //Core fine line
    cl = S(t*.3 + PIXEL, t*.3, d),
    //Glow
    g = PIXEL / (d + 1e-3),

    //Random Offset/Speed
    ro = sin(an*7.1)*2.5 + cos(an*3.1)*2.,
    rs = 1.5 + sin(an*5.)*.5,
    //Phase
    w = fract(di + ro - T*rs),
    //Pulse
    pu = .1 / (1. - w + .015) * hash(uv);
    pu = max(0., pu - .05) * S(1., .95, w);
    //Base border col
    vec3 bc = vec3(0.2941, 0.33, 0.388)*l*1.5,
    pc = vec3(.973, .443, .443)*pu*cl*4.;
    pc += vec3(1., .7, 0.) * pu * g ;

    vec3 c = bc + pc;
    c *= exp(-di * .5);

    fragColor = vec4(c, 1.);
}
