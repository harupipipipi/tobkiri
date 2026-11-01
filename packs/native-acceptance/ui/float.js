/** Convert a finite WIT-width number to the frozen Rust {:e} string form. */
export const toCanonFloat = (input, width) => {
  if (width !== "f32" && width !== "f64") throw new TypeError("float width must be f32 or f64");
  let value = Number(input);
  if (!Number.isFinite(value)) throw new TypeError("non-finite WIT float");
  if (width === "f32") value = Math.fround(value);
  if (!Number.isFinite(value)) throw new TypeError("WIT f32 overflow");
  if (Object.is(value, -0)) return "-0e0";
  if (value === 0) return "0e0";
  let decimal = value.toString();
  if (width === "f32") {
    // Choose the shortest decimal which round-trips at the declared width.
    // IEEE binary32 requires at most nine significant decimal digits.
    for (let precision = 1; precision <= 9; precision++) {
      const candidate = value.toPrecision(precision);
      if (Object.is(Math.fround(Number(candidate)), value)) { decimal = candidate; break; }
    }
  }
  const negative = decimal.startsWith("-");
  if (negative) decimal = decimal.slice(1);
  const [mantissa, exponentText = "0"] = decimal.toLowerCase().split("e");
  const point = mantissa.indexOf(".");
  const integerDigits = point === -1 ? mantissa.length : point;
  let digits = mantissa.replace(".", "");
  const leading = digits.search(/[1-9]/);
  let exponent = Number(exponentText) + integerDigits - leading - 1;
  digits = digits.slice(leading).replace(/0+$/, "");
  const normalized = digits.length === 1 ? digits : `${digits[0]}.${digits.slice(1)}`;
  return `${negative ? "-" : ""}${normalized}e${exponent}`;
};
