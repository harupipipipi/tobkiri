type TitleBarProps = {
  appName?: string;
  appIcon?: string;
};

export function TitleBar(_props: TitleBarProps) {
  // The browser or desktop shell already provides window chrome. Keep the
  // renderer slot available without adding a second title strip to the page.
  return null;
}
