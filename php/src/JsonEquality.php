<?php

declare(strict_types=1);

namespace Rustwright;

final class JsonEquality
{
    public static function equals(mixed $left, mixed $right): bool
    {
        if ((is_int($left) || is_float($left)) && (is_int($right) || is_float($right))) {
            return (float) $left === (float) $right;
        }
        if (is_array($left) || is_array($right)) {
            if (!is_array($left) || !is_array($right) || count($left) !== count($right)) {
                return false;
            }
            foreach ($left as $index => $value) {
                if (!array_key_exists($index, $right) || !self::equals($value, $right[$index])) {
                    return false;
                }
            }
            return true;
        }
        if ($left instanceof \stdClass || $right instanceof \stdClass) {
            if (!$left instanceof \stdClass || !$right instanceof \stdClass) {
                return false;
            }
            $leftFields = get_object_vars($left);
            $rightFields = get_object_vars($right);
            if (count($leftFields) !== count($rightFields)) {
                return false;
            }
            foreach ($leftFields as $key => $value) {
                if (!array_key_exists($key, $rightFields) || !self::equals($value, $rightFields[$key])) {
                    return false;
                }
            }
            return true;
        }
        return $left === $right;
    }
}
