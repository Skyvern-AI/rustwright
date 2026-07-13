// Template for future sitespeed.io/Browsertime user journeys.
// Wire this into the runner only after validating the exact sitespeed.io script
// API version in the installed package.
module.exports = async function basicJourney(context, commands) {
  await commands.navigate('https://example.com/');
  await commands.measure.start('example-page');
  await commands.wait.byTime(1000);
  return commands.measure.stop();
};
