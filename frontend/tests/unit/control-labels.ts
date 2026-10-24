import { expect } from "vitest";

// Browser label locators read the associated label's text content. Keeping the
// label outside the control prevents option names and textarea values from
// becoming part of that name. This validates actual rendered markup, not JSX.
export function expectControlLabel(
  html: string,
  name: string,
  tag: "select" | "textarea" | "input",
) {
  const markup = html.replaceAll("<!-- -->", "");
  const labels = [
    ...markup.matchAll(/<label\b[^>]*for="([^"]+)"[^>]*>(.*?)<\/label>/gs),
  ];
  const matching = labels.filter((entry) => entry[2] === name);
  expect(matching, `One exact standalone label for ${name}`).toHaveLength(1);
  const id = matching[0][1];
  expect(
    [...markup.matchAll(/\bid="([^"]+)"/g)].filter((entry) => entry[1] === id),
    `Unique control ID for ${name}`,
  ).toHaveLength(1);
  expect(markup).toMatch(new RegExp(`<${tag}\\b[^>]*id="${id}"`));
}
