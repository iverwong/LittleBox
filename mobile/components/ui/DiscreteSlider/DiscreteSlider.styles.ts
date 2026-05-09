import { StyleSheet } from 'react-native';
import type { ViewStyle, TextStyle } from 'react-native';
import type { Theme } from '@/theme';

type DiscreteSliderStyles = {
  container: ViewStyle;
  centerLabelRow: ViewStyle;
  trackRow: ViewStyle;
  trackRowDisabled: ViewStyle;
  trackBg: ViewStyle;
  trackBgDisabled: ViewStyle;
  activeTrack: ViewStyle;
  activeTrackDisabled: ViewStyle;
  nodeDot: ViewStyle;
  nodeDotDisabled: ViewStyle;
  node: ViewStyle;
  thumbOuter: ViewStyle;
  thumbOuterDisabled: ViewStyle;
  thumb: ViewStyle;
  thumbDisabled: ViewStyle;
  bottomRow: ViewStyle;
  centerValue: TextStyle;
  centerDesc: TextStyle;
  leftLabelText: TextStyle;
  rightLabelText: TextStyle;
};

export const createStyles = (theme: Theme): DiscreteSliderStyles => {
  return StyleSheet.create({
    container: {
      paddingVertical: 20,
      paddingHorizontal: 24,
      borderRadius: theme.radius.lg,
      backgroundColor: theme.palette.neutral[100],
      ...theme.shadow.sm,
    },
    centerLabelRow: {
      minHeight: 24,
      alignItems: 'center',
      justifyContent: 'flex-end',
      marginBottom: 4,
    },
    trackRow: {
      height: 44,
      justifyContent: 'center',
    },
    trackRowDisabled: {
      opacity: 0.6,
    },
    trackBg: {
      height: 6,
      backgroundColor: theme.palette.neutral[200],
      borderRadius: 3,
    },
    trackBgDisabled: {
      backgroundColor: theme.palette.neutral[200],
    },
    activeTrack: {
      position: 'absolute',
      left: 0,
      top: '50%',
      marginTop: -3,
      height: 6,
      backgroundColor: theme.palette.primary[300],
      borderRadius: 3,
    },
    activeTrackDisabled: {
      backgroundColor: theme.palette.neutral[200],
    },
    nodeDot: {
      position: 'absolute',
      width: 14,
      height: 14,
      borderRadius: 7,
      backgroundColor: theme.palette.primary[500],
      borderWidth: 2,
      borderColor: theme.palette.neutral[50],
      marginLeft: -7,
      top: '50%',
      marginTop: -7,
    },
    nodeDotDisabled: {
      backgroundColor: theme.palette.neutral[500],
    },
    node: {
      width: 14,
      height: 14,
      borderRadius: 7,
      backgroundColor: theme.palette.primary[500],
      borderWidth: 2,
      borderColor: theme.palette.neutral[50],
    },
    thumbOuter: {
      position: 'absolute',
      left: 0,
      top: '50%',
      marginTop: -15,
      width: 30,
      height: 30,
      borderRadius: 15,
      backgroundColor: theme.surface.paper,
      alignItems: 'center',
      justifyContent: 'center',
      zIndex: 1,
      elevation: 1,
      // shadow handled via shadow.sm on thumb
    },
    thumbOuterDisabled: {
      backgroundColor: theme.surface.paper,
    },
    thumb: {
      width: 24,
      height: 24,
      borderRadius: 12,
      backgroundColor: theme.palette.primary[500],
      ...theme.shadow.sm,
    },
    thumbDisabled: {
      backgroundColor: theme.palette.neutral[500],
    },
    bottomRow: {
      flexDirection: 'row',
      justifyContent: 'space-between',
      marginTop: -8,
    },
    centerValue: {
      fontSize: theme.typography.fontSize.lg,
      fontWeight: theme.typography.fontWeight.semibold,
      color: theme.palette.neutral[900],
    },
    centerDesc: {
      fontSize: 13,
      color: theme.palette.neutral[500],
      marginTop: 2,
    },
    leftLabelText: {
      fontSize: theme.typography.fontSize.xs,
      color: theme.palette.neutral[500],
    },
    rightLabelText: {
      fontSize: theme.typography.fontSize.xs,
      color: theme.palette.neutral[500],
    },
  });
};
