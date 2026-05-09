/**
 * Typed accessor for tokens.json. Components should reference token names,
 * not raw hex values. CSS uses the variables in tokens.css.
 */
import tokens from "../tokens.json";

export type Tokens = typeof tokens;
export const t: Tokens = tokens;
export default t;
